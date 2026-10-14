import { useCallback, useMemo, useRef, useState } from "react";
import {
  ApiError,
  createKey,
  getDistill,
  getProviders,
  getRecent,
  getTimeseries,
  getUsage,
  listKeys,
  setKeyActive,
  setOutage,
  type ApiKey,
  type DistillAuditEvent,
  type DistillTask,
  type ProviderStatus,
  type RecentRequest,
  type Timeseries,
  type UsageRow,
} from "../api";
import { ago, count, ms, pct, time, usd } from "../format";
import { usePolling, useStored } from "../hooks";
import { useToast } from "./toast";
import {
  IconActivity,
  IconAlert,
  IconCheck,
  IconClock,
  IconCoins,
  IconDatabase,
  IconFlask,
  IconKey,
  IconLayers,
  IconLock,
  IconLogOut,
  IconPlus,
  IconServer,
  IconShuffle,
  IconX,
  IconZap,
} from "./icons";
import { fillBuckets, TimeChart } from "./TimeChart";
import {
  Badge,
  Callout,
  Card,
  CopyButton,
  EmptyState,
  Meter,
  Segmented,
  Skeleton,
  SkeletonRows,
  Sparkline,
  StatusDot,
  Ticker,
  type Tone,
} from "./ui";

const compactFmt = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const compact = (v: number) => compactFmt.format(Math.round(v));
const axisUsd = (v: number) => (v === 0 ? "$0" : v < 0.01 ? usd(v) : `$${v.toFixed(2)}`);

const RANGES = [
  { label: "1h", title: "Last hour", hours: 1, bucket: "minute" },
  { label: "24h", title: "Last 24 hours", hours: 24, bucket: "hour" },
  { label: "7d", title: "Last 7 days", hours: 168, bucket: "hour" },
] as const;

type Distill = Awaited<ReturnType<typeof getDistill>>;

export function Dashboard() {
  const [token, setToken] = useStored("gw.admin", "", "session");
  if (!token) return <AdminGate onToken={setToken} />;
  return <Console token={token} onSignOut={() => setToken("")} />;
}

function AdminGate({ onToken }: { onToken: (t: string) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await getProviders(value);
      onToken(value);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="gate">
      <form className="card gate__card" onSubmit={(e) => void submit(e)}>
        <span className="gate__icon">
          <IconLock size={20} />
        </span>
        <h1 className="gate__title">Admin sign-in</h1>
        <p className="gate__sub">
          The dashboard uses the gateway's <code>ADMIN_TOKEN</code>. It's kept for this browser tab only.
        </p>
        <label className="sr-only" htmlFor="admin-token">
          Admin token
        </label>
        <input
          id="admin-token"
          className="input input--mono"
          type="password"
          placeholder="Admin token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
          autoFocus
        />
        <button className="btn btn--primary btn--block" type="submit" disabled={!value || busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Open dashboard
        </button>
        {error && <Callout tone="bad">{error}</Callout>}
      </form>
    </div>
  );
}

function Console({ token, onSignOut }: { token: string; onSignOut: () => void }) {
  const [rangeIndex, setRangeIndex] = useState(1);
  const range = RANGES[rangeIndex]!;
  const [series, setSeries] = useState<Timeseries | null>(null);
  const [usage, setUsage] = useState<UsageRow[] | null>(null);
  const [recent, setRecent] = useState<RecentRequest[] | null>(null);
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [distill, setDistill] = useState<Distill>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const toast = useToast();

  const guard = useCallback(
    async (fn: () => Promise<void>) => {
      try {
        await fn();
        setError(null);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) onSignOut();
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [onSignOut],
  );

  const refreshSlow = useCallback(
    () =>
      guard(async () => {
        const ts = await getTimeseries(token, range.hours, range.bucket);
        setSeries(ts);
        const [u, k] = await Promise.all([getUsage(token, ts.since), listKeys(token)]);
        setUsage(u);
        setKeys(k);
      }),
    [guard, token, range.hours, range.bucket],
  );
  const refreshFast = useCallback(
    () =>
      guard(async () => {
        // Distill is optional: its failure must never blank the live feed or provider health.
        const distillReq = getDistill(token).then(setDistill, () => setDistill(null));
        const [r, p] = await Promise.all([getRecent(token), getProviders(token)]);
        setRecent(r.rows);
        setProviders(p);
        await distillReq;
      }),
    [guard, token],
  );
  usePolling(refreshSlow, 15_000);
  usePolling(refreshFast, 3_000);

  const keyNames = useMemo(() => new Map((keys ?? []).map((k) => [k.id, k.name])), [keys]);

  function changeRange(i: number) {
    setRangeIndex(i);
    setSeries(null);
    setUsage(null);
  }

  return (
    <div className="dashboard">
      <div className="page-head">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-sub">
            Traffic, spend and reliability across every key, model and provider.
          </p>
        </div>
        <div className="page-head__actions">
          {series && (
            <span
              className="source-pill"
              title="Dashboard reads go to the streaming read replica; spend enforcement never does."
            >
              <IconDatabase size={13} />
              {series.source}
              {series.replica_lag_s !== null && (
                <span className="source-pill__lag">lag ≤ {series.replica_lag_s.toFixed(1)}s</span>
              )}
            </span>
          )}
          <Segmented
            label="Time range"
            value={rangeIndex}
            onChange={changeRange}
            options={RANGES.map((r, i) => ({ value: i, label: r.label, title: r.title }))}
          />
          <button type="button" className="btn btn--ghost" onClick={onSignOut}>
            <IconLogOut />
            <span className="hide-sm">Sign out</span>
          </button>
        </div>
      </div>

      {error && (
        <Callout tone="bad" title="Couldn't refresh the dashboard" onDismiss={() => setError(null)}>
          {error}
        </Callout>
      )}

      <Kpis series={series} rangeTitle={range.title} />
      <Charts series={series} />

      <div className="grid-2">
        <ProvidersPanel
          providers={providers}
          pending={pending}
          onToggle={(name, down) =>
            guard(async () => {
              setPending(name);
              try {
                await setOutage(token, name, down);
                setProviders(await getProviders(token));
                toast(
                  down ? "info" : "ok",
                  down ? `${name} taken down` : `${name} restored`,
                  down ? "All traffic now fails over along its fallback chains." : "Back in rotation.",
                );
              } finally {
                setPending(null);
              }
            })
          }
        />
        <UsagePanel rows={usage} keyNames={keyNames} />
      </div>

      {distill && <DistillPanel tasks={distill.tasks} audit={distill.audit} />}
      <RecentPanel rows={recent} />
      <KeysPanel
        keys={keys}
        onCreate={async (k) => {
          let raw = "";
          await guard(async () => {
            raw = (await createKey(token, k)).key;
            setKeys(await listKeys(token));
            toast("ok", `Key “${k.name}” created`, "Copy it now; it is shown only once.");
          });
          return raw;
        }}
        onToggle={(id, active) =>
          guard(async () => {
            await setKeyActive(token, id, active);
            setKeys(await listKeys(token));
            toast(
              active ? "ok" : "info",
              active ? "Key reactivated" : "Key revoked",
              active ? undefined : "Cached key lookups may still accept it for up to 60 s.",
            );
          })
        }
      />
    </div>
  );
}

// ---- KPIs and charts ----------------------------------------------------------------------

function Kpis({ series, rangeTitle }: { series: Timeseries | null; rangeTitle: string }) {
  const t = series?.totals ?? {};
  const requests = t.requests ?? 0;
  const errorRate = requests ? (t.errors ?? 0) / requests : null;
  const hitRate = requests ? (t.cache_hits ?? 0) / requests : null;
  const int = (v: number) => count(Math.round(v));
  const items: {
    label: string;
    value: number | null;
    format: (v: number) => string;
    icon: React.ReactNode;
    sub?: string;
    tone?: Tone;
    spark?: number[];
  }[] = [
    {
      label: "Requests",
      value: t.requests ?? null,
      format: int,
      icon: <IconActivity />,
      sub: rangeTitle,
      spark: series?.rows.map((r) => r.requests),
    },
    {
      label: "Spend",
      value: t.cost_usd === undefined ? null : Number(t.cost_usd),
      format: (v) => usd(v),
      icon: <IconCoins />,
      sub: `${count(t.tokens)} tokens`,
      spark: series?.rows.map((r) => Number(r.cost_usd)),
    },
    {
      label: "Cache hit rate",
      value: hitRate,
      format: (v) => pct(v),
      icon: <IconDatabase />,
      sub: `${count(t.cache_hits)} hits`,
    },
    {
      label: "Error rate",
      value: errorRate,
      format: (v) => pct(v),
      icon: <IconAlert />,
      sub: `${count(t.errors)} errors`,
      tone: errorRate !== null && errorRate > 0.05 ? "bad" : undefined,
    },
    { label: "Failovers", value: t.fallbacks ?? null, format: int, icon: <IconShuffle />, sub: "fallback hops" },
    {
      label: "Latency p50",
      value: t.p50_latency_ms ?? null,
      format: (v) => ms(v),
      icon: <IconClock />,
      sub: `p95 ${ms(t.p95_latency_ms)}`,
      spark: series?.rows.map((r) => r.p50_latency_ms ?? 0),
    },
  ];
  return (
    <div className="kpis" aria-busy={!series}>
      {items.map((k) => (
        <div key={k.label} className={`kpi ${k.tone ? `kpi--${k.tone}` : ""}`}>
          <span className="kpi__label">
            {k.icon}
            {k.label}
          </span>
          {series ? (
            <>
              <strong className="kpi__value">
                <Ticker value={k.value} format={k.format} />
              </strong>
              <span className="kpi__foot">
                {k.sub && <span className="kpi__sub">{k.sub}</span>}
                {k.spark && <Sparkline values={k.spark} label={`${k.label} trend`} />}
              </span>
            </>
          ) : (
            <>
              <Skeleton width="60%" height={24} className="kpi__skel" />
              <Skeleton width="40%" height={10} />
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function ChartSkeleton() {
  return (
    <div className="chart-skeleton" aria-hidden="true">
      {Array.from({ length: 18 }, (_, i) => (
        <span key={i} style={{ height: `${20 + ((i * 37) % 60)}%` }} />
      ))}
    </div>
  );
}

function Charts({ series }: { series: Timeseries | null }) {
  const points = useMemo(() => {
    if (!series) return [];
    const rows = series.rows.map((r) => ({
      bucket: r.bucket,
      values: {
        ok: r.requests - r.errors,
        errors: r.errors,
        cost: Number(r.cost_usd),
        p50: r.p50_latency_ms ?? 0,
        p95: r.p95_latency_ms ?? 0,
      },
    }));
    return fillBuckets(rows, new Date(series.since), new Date(series.until), series.bucket);
  }, [series]);

  const charts = [
    {
      title: "Requests",
      icon: <IconActivity />,
      total: series ? count(series.totals.requests) : undefined,
      chart: series && (
        <TimeChart
          label="Requests over time"
          points={points}
          bucket={series.bucket}
          format={compact}
          series={[
            { key: "ok", label: "Succeeded", color: "var(--chart-1)", kind: "bar" },
            { key: "errors", label: "Errors", color: "var(--chart-bad)", kind: "bar" },
          ]}
        />
      ),
    },
    {
      title: "Spend",
      icon: <IconCoins />,
      total: series ? usd(series.totals.cost_usd) : undefined,
      chart: series && (
        <TimeChart
          label="Spend over time"
          points={points}
          bucket={series.bucket}
          format={axisUsd}
          series={[{ key: "cost", label: "Cost (USD)", color: "var(--chart-2)", kind: "bar" }]}
        />
      ),
    },
    {
      title: "Latency",
      icon: <IconClock />,
      total: series ? `p95 ${ms(series.totals.p95_latency_ms)}` : undefined,
      chart: series && (
        <TimeChart
          label="Latency percentiles over time"
          points={points}
          bucket={series.bucket}
          format={(v) => ms(v)}
          series={[
            { key: "p50", label: "p50", color: "var(--chart-1)", kind: "line" },
            { key: "p95", label: "p95", color: "var(--chart-3)", kind: "line" },
          ]}
        />
      ),
    },
  ];

  return (
    <div className="grid-3">
      {charts.map((c) => (
        <Card
          key={c.title}
          title={c.title}
          icon={c.icon}
          actions={
            c.total !== undefined ? (
              <span className="chart-total">{c.total}</span>
            ) : (
              <Skeleton width={56} height={14} />
            )
          }
        >
          {c.chart || <ChartSkeleton />}
        </Card>
      ))}
    </div>
  );
}

// ---- providers ----------------------------------------------------------------------------

const STATE_LABEL: Record<string, [string, Tone]> = {
  closed: ["Healthy", "ok"],
  half_open: ["Recovering", "warn"],
  open: ["Circuit open", "bad"],
  forced_down: ["Taken down", "bad"],
  not_configured: ["No API key", "neutral"],
};

function ProvidersPanel({
  providers,
  pending,
  onToggle,
}: {
  providers: ProviderStatus[] | null;
  pending: string | null;
  onToggle: (name: string, down: boolean) => void;
}) {
  return (
    <Card
      title="Provider health"
      icon={<IconServer />}
      description="Taking a provider down affects all traffic: requests fail over along their fallback chains, exactly as in a real outage."
    >
      {providers === null ? (
        <SkeletonRows rows={4} cols={3} />
      ) : providers.length === 0 ? (
        <EmptyState compact icon={<IconServer size={18} />} title="No providers configured" />
      ) : (
        <ul className="providers">
          {providers.map((p) => {
            const [label, tone] = STATE_LABEL[p.state] ?? [p.state, "neutral" as Tone];
            const down = p.state === "forced_down";
            const busy = pending === p.name;
            return (
              <li key={p.name} className="provider">
                <StatusDot tone={tone} label={label} />
                <div className="provider__main">
                  <span className="provider__name">{p.name}</span>
                  <span className="provider__aliases" title={p.aliases.join(", ")}>
                    {p.aliases.length ? p.aliases.join(" · ") : "no aliases"}
                  </span>
                </div>
                <Badge tone={tone}>{label}</Badge>
                <button
                  type="button"
                  className={`btn btn--sm ${down ? "btn--secondary" : "btn--danger"}`}
                  disabled={!p.configured || busy}
                  onClick={() => onToggle(p.name, !down)}
                  aria-label={`${down ? "Restore" : "Take down"} ${p.name}`}
                >
                  {busy && <span className="spinner" aria-hidden="true" />}
                  {down ? "Restore" : "Take down"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

// ---- usage --------------------------------------------------------------------------------

function UsagePanel({ rows, keyNames }: { rows: UsageRow[] | null; keyNames: Map<string, string> }) {
  const maxCost = Math.max(0, ...(rows ?? []).map((r) => Number(r.cost_usd)));
  return (
    <Card title="Usage by key and model" icon={<IconLayers />} flush>
      {rows === null ? (
        <div className="card__pad">
          <SkeletonRows rows={5} cols={6} />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState compact icon={<IconLayers size={18} />} title="No traffic in this range yet">
          Send a request from the Playground and it will show up here.
        </EmptyState>
      ) : (
        <div className="table-wrap table-wrap--tall">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Model</th>
                <th scope="col" className="num">
                  Requests
                </th>
                <th scope="col" className="num">
                  Cache hits
                </th>
                <th scope="col" className="num">
                  p95
                </th>
                <th scope="col" className="num">
                  Cost
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.key_id}-${r.alias}`}>
                  <td className="cell-strong cell-wrap">{(r.key_id && keyNames.get(r.key_id)) ?? "—"}</td>
                  <td>
                    <code className="code-pill">{r.alias}</code>
                  </td>
                  <td className="num">{count(r.requests)}</td>
                  <td className="num">{count(r.cache_hits)}</td>
                  <td className="num">{ms(r.p95_latency_ms)}</td>
                  <td className="num">
                    <span className="cost-cell">
                      <span className="cost-cell__track" aria-hidden="true">
                        <span
                          className="cost-cell__bar"
                          style={{ width: `${maxCost ? (Number(r.cost_usd) / maxCost) * 100 : 0}%` }}
                        />
                      </span>
                      {usd(r.cost_usd)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---- distilled models ---------------------------------------------------------------------

const ROLLOUT: Record<string, [string, Tone]> = {
  off: ["Registered", "neutral"],
  shadow: ["Shadow", "purple"],
  canary: ["Canary", "warn"],
  full: ["Full", "ok"],
  rolled_back: ["Rolled back", "bad"],
};

const PIPELINE = [
  { id: "capture", label: "Capture", hint: "via Kafka" },
  { id: "discover", label: "Discover", hint: "repeated task" },
  { id: "dataset", label: "Dataset", hint: "strong-model labels" },
  { id: "train", label: "Train", hint: "small model" },
  { id: "eval", label: "Eval gate", hint: "offline check" },
  { id: "shadow", label: "Shadow", hint: "compare in background" },
  { id: "canary", label: "Canary", hint: "partial traffic" },
  { id: "full", label: "Full", hint: "all traffic" },
] as const;

type StepState = "done" | "current" | "todo" | "warn" | "failed";

interface Step {
  id: string;
  label: string;
  caption: string;
  state: StepState;
  when?: string;
}

/** Where a task sits in the pipeline, from its rollout state plus its audit trail (newest first). */
function pipelineFor(t: DistillTask, audit: DistillAuditEvent[]): Step[] {
  const events = audit.filter((e) => e.task === t.task);
  const gate = events
    .map((e) => /eval gate:\s*(pass|fail)/i.exec(e.reason)?.[1]?.toUpperCase())
    .find((g): g is string => Boolean(g));
  const rollout = ["shadow", "canary", "full"];
  let reached = rollout.indexOf(t.state); // -1 for off / rolled_back
  let failedAt = -1;
  if (t.state === "rolled_back") {
    const before = events.find((e) => e.state === "canary" || e.state === "full");
    failedAt = before ? rollout.indexOf(before.state) : 1;
    reached = failedAt;
  }
  const registered = events.find((e) => e.action === "register");
  return PIPELINE.map((p, i): Step => {
    if (i < 4) {
      return { id: p.id, label: p.label, caption: p.id === "train" ? `version ${t.version}` : p.hint, state: "done" };
    }
    if (p.id === "eval") {
      return {
        id: p.id,
        label: p.label,
        caption: gate ? `gate ${gate}${gate === "FAIL" && reached >= 0 ? " · overridden" : ""}` : "passed",
        state: gate === "FAIL" ? "warn" : t.state === "off" ? "current" : "done",
        when: registered ? ago(registered.ts) : undefined,
      };
    }
    const r = rollout.indexOf(p.id);
    const ev = events.find((e) => e.state === p.id);
    const caption =
      p.id === "shadow" && t.shadow_n
        ? `${t.shadow_agree}/${t.shadow_n} agree`
        : p.id === "canary" && t.state === "canary"
          ? `${Math.round(t.weight * 100)}% traffic`
          : p.hint;
    const state: StepState = r === failedAt ? "failed" : r < reached ? "done" : r === reached ? "current" : "todo";
    return { id: p.id, label: p.label, caption, state, when: ev ? ago(ev.ts) : undefined };
  });
}

function Pipeline({ steps }: { steps: Step[] }) {
  return (
    <ol className="pipeline" aria-label="Distillation pipeline">
      {steps.map((s, i) => (
        <li key={s.id} className={`pipeline__step is-${s.state}`} style={{ animationDelay: `${i * 60}ms` }}>
          <span className="pipeline__dot" aria-hidden="true">
            {s.state === "done" && <IconCheck size={11} />}
            {s.state === "failed" && <IconX size={11} />}
            {s.state === "warn" && <IconAlert size={11} />}
          </span>
          <span className="pipeline__text">
            <span className="pipeline__label">{s.label}</span>
            <span className="pipeline__caption">{s.caption}</span>
            {s.when && <span className="pipeline__when">{s.when}</span>}
          </span>
          <span className="sr-only">
            {s.state === "current" ? "current stage" : s.state === "todo" ? "not reached" : s.state}
          </span>
        </li>
      ))}
    </ol>
  );
}

function DistillPanel({ tasks, audit }: { tasks: DistillTask[]; audit: DistillAuditEvent[] }) {
  const live = tasks.filter((t) => t.state === "canary" || t.state === "full").length;
  const shadow = tasks.filter((t) => t.state === "shadow").length;
  return (
    <Card
      title="Distillation pipeline"
      icon={<IconFlask />}
      description="Repeated tasks are captured, distilled into small models, and rolled out shadow → canary → full. Canary and full serve real traffic and roll back automatically when the failure rate rises."
      actions={
        <>
          {shadow > 0 && (
            <Badge tone="purple" dot>
              {shadow} in shadow
            </Badge>
          )}
          <Badge tone={live ? "ok" : "neutral"} dot>
            {live} serving
          </Badge>
        </>
      }
    >
      {tasks.length === 0 ? (
        <EmptyState compact icon={<IconFlask size={18} />} title="No distilled tasks registered">
          Build and register a distilled model to start a shadow rollout.
        </EmptyState>
      ) : (
        <div className="distill-list">
          {tasks.map((t) => {
            const [label, tone] = ROLLOUT[t.state] ?? [t.state, "neutral" as Tone];
            const agree = t.shadow_n ? t.shadow_agree / t.shadow_n : null;
            const valid = t.shadow_n ? t.shadow_valid / t.shadow_n : null;
            const fail = t.window_n ? t.window_failures / t.window_n : null;
            return (
              <article key={t.system_fingerprint} className="distill-task">
                <header className="distill-task__head">
                  <div className="distill-task__titles">
                    <h3 className="distill-task__name">{t.task}</h3>
                    <span className="distill-task__alias">
                      <code>{t.distilled_alias}</code>
                      <span className="muted"> learns from </span>
                      <code>{t.source_alias}</code>
                    </span>
                  </div>
                  <div className="distill-task__badges">
                    <code className="code-pill" title="Version · system prompt fingerprint">
                      {t.version} · {t.system_fingerprint.slice(0, 8)}
                    </code>
                    <Badge tone={tone} dot>
                      {label}
                    </Badge>
                  </div>
                </header>
                <Pipeline steps={pipelineFor(t, audit)} />
                <dl className="distill-stats">
                  <div>
                    <dt>Traffic served</dt>
                    <dd>
                      <strong>{(t.weight * 100).toFixed(0)}%</strong>
                      <Meter value={t.weight} tone="accent" label="Traffic share" />
                    </dd>
                  </div>
                  <div>
                    <dt>Shadow agreement</dt>
                    <dd>
                      <strong>{agree === null ? "—" : pct(agree)}</strong>
                      <span className="muted small">
                        {t.shadow_agree}/{t.shadow_n}
                      </span>
                      <Meter value={agree} tone={agree !== null && agree < 0.9 ? "warn" : "ok"} label="Shadow agreement" />
                    </dd>
                  </div>
                  <div>
                    <dt>Valid outputs</dt>
                    <dd>
                      <strong>{valid === null ? "—" : pct(valid)}</strong>
                      <span className="muted small">
                        {t.shadow_valid}/{t.shadow_n}
                      </span>
                      <Meter value={valid} tone="ok" label="Valid output rate" />
                    </dd>
                  </div>
                  <div>
                    <dt>Online failures · 10 min</dt>
                    <dd>
                      <strong>{fail === null ? "—" : pct(fail)}</strong>
                      <span className="muted small">of {count(t.window_n)}</span>
                      <Meter value={fail} tone={fail !== null && fail > 0.05 ? "bad" : "ok"} label="Online failure rate" />
                    </dd>
                  </div>
                </dl>
              </article>
            );
          })}
        </div>
      )}
      {audit.length > 0 && (
        <div className="audit">
          <h3 className="section-label">Rollout history</h3>
          <ol className="timeline">
            {audit.map((e, i) => {
              const tone = ROLLOUT[e.state]?.[1] ?? "neutral";
              return (
                <li key={`${e.ts}-${i}`} className={`timeline__item timeline__item--${tone}`}>
                  <span className="timeline__dot" aria-hidden="true" />
                  <div className="timeline__body">
                    <div className="timeline__line">
                      <strong>{e.task}</strong>
                      <span className="muted">{e.action}</span>
                      <span aria-hidden="true" className="muted">
                        →
                      </span>
                      <Badge tone={tone}>{ROLLOUT[e.state]?.[0] ?? e.state}</Badge>
                      {e.weight > 0 && <span className="muted">{(e.weight * 100).toFixed(0)}%</span>}
                    </div>
                    <div className="timeline__meta">
                      <time dateTime={e.ts} title={new Date(e.ts).toLocaleString()}>
                        {ago(e.ts)}
                      </time>{" "}
                      · by {e.actor}
                      {e.reason && <> · {e.reason}</>}
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </Card>
  );
}

// ---- live requests ------------------------------------------------------------------------

function RecentPanel({ rows }: { rows: RecentRequest[] | null }) {
  // Rows that arrived since the previous poll get a brief highlight (not on first load).
  const seen = useRef<Set<string> | null>(null);
  const fresh = useMemo(() => {
    if (!rows) return new Set<string>();
    const prev = seen.current;
    seen.current = new Set(rows.map((r) => r.id));
    return prev ? new Set(rows.filter((r) => !prev.has(r.id)).map((r) => r.id)) : new Set<string>();
  }, [rows]);
  return (
    <Card
      title="Live requests"
      icon={<IconZap />}
      flush
      actions={
        <span className="live" title="Refreshes every 3 seconds">
          <StatusDot tone="ok" pulse />
          Live
        </span>
      }
    >
      {rows === null ? (
        <div className="card__pad">
          <SkeletonRows rows={6} cols={7} />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState compact icon={<IconZap size={18} />} title="Waiting for requests">
          Send one from the Playground; it appears here within 3 seconds.
        </EmptyState>
      ) : (
        <div className="table-wrap table-wrap--tall">
          <table className="table table--sticky">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Key</th>
                <th scope="col">Requested</th>
                <th scope="col">Served by</th>
                <th scope="col">Route</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">
                  Latency
                </th>
                <th scope="col" className="num">
                  Cost
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={fresh.has(r.id) ? "is-new" : undefined}>
                  <td title={new Date(r.created_at).toLocaleString()}>
                    <div className="cell-stack">
                      <span className="tabular">{time(r.created_at)}</span>
                      <span className="cell-sub">{ago(r.created_at)}</span>
                    </div>
                  </td>
                  <td>{r.key_name ?? <span className="muted">—</span>}</td>
                  <td>
                    <code className="code-pill">{r.alias}</code>
                  </td>
                  <td>
                    <div className="cell-stack">
                      <span>
                        <span className="cell-strong">{r.provider}</span>{" "}
                        <span className="cell-sub">{r.upstream_model}</span>
                      </span>
                      {(r.fallback_count > 0 || r.cache_hit) && (
                        <span className="cell-badges">
                          {r.fallback_count > 0 && <Badge tone="warn">failover ×{r.fallback_count}</Badge>}
                          {r.cache_hit && <Badge tone="ok">cache</Badge>}
                        </span>
                      )}
                    </div>
                  </td>
                  <td>
                    {r.route ? (
                      <Badge tone={r.route === "cheap" ? "ok" : "purple"}>
                        {r.route}
                        {r.route_probability !== null && ` ${r.route_probability.toFixed(2)}`}
                      </Badge>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <Badge tone={r.status === "ok" ? "ok" : "bad"} dot>
                      {r.status === "ok" ? "ok" : (r.error_type ?? r.status)}
                    </Badge>
                  </td>
                  <td className="num">{ms(r.latency_ms)}</td>
                  <td className="num">{usd(r.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---- API keys -----------------------------------------------------------------------------

function KeysPanel({
  keys,
  onCreate,
  onToggle,
}: {
  keys: ApiKey[] | null;
  onCreate: (k: {
    name: string;
    rpm_limit?: number;
    monthly_spend_cap_usd?: string;
    routing_quality: string;
  }) => Promise<string>;
  onToggle: (id: string, active: boolean) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [rpm, setRpm] = useState("60");
  const [cap, setCap] = useState("1");
  const [quality, setQuality] = useState("balanced");
  const [created, setCreated] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const visible = (keys ?? [])
    .filter((k) => showRevoked || k.active)
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
  const shown = showAll ? visible : visible.slice(0, 10);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    try {
      const raw = await onCreate({
        name,
        rpm_limit: rpm ? Number(rpm) : undefined,
        monthly_spend_cap_usd: cap || undefined,
        routing_quality: quality,
      });
      if (raw) {
        setCreated(raw);
        setName("");
      }
    } finally {
      setCreating(false);
    }
  }

  async function toggle(k: ApiKey) {
    setToggling(k.id);
    try {
      await onToggle(k.id, !k.active);
    } finally {
      setToggling(null);
    }
  }

  return (
    <Card
      title="API keys"
      icon={<IconKey />}
      description="Keys are stored as SHA-256 hashes; the raw key is shown once at creation."
      actions={
        <label className="check">
          <input type="checkbox" checked={showRevoked} onChange={(e) => setShowRevoked(e.target.checked)} />
          Show revoked
        </label>
      }
      flush
    >
      <div className="card__pad keys-top">
        <form className="key-form" onSubmit={(e) => void submit(e)}>
          <div className="field">
            <label className="field__label" htmlFor="k-name">
              Name
            </label>
            <input
              id="k-name"
              className="input"
              placeholder="e.g. mobile-app"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="k-rpm">
              Requests / min
            </label>
            <input
              id="k-rpm"
              className="input"
              inputMode="numeric"
              placeholder="unlimited"
              value={rpm}
              onChange={(e) => setRpm(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="k-cap">
              Monthly cap ($)
            </label>
            <input
              id="k-cap"
              className="input"
              inputMode="decimal"
              placeholder="unlimited"
              value={cap}
              onChange={(e) => setCap(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="k-quality">
              Auto routing
            </label>
            <div className="select">
              <select id="k-quality" value={quality} onChange={(e) => setQuality(e.target.value)}>
                <option value="low">Save most</option>
                <option value="balanced">Balanced</option>
                <option value="high">Best quality</option>
              </select>
            </div>
          </div>
          <button className="btn btn--primary key-form__submit" type="submit" disabled={creating || !name}>
            {creating ? <span className="spinner" aria-hidden="true" /> : <IconPlus />}
            Create key
          </button>
        </form>

        {created && (
          <Callout
            tone="ok"
            title="Key created. Copy it now; it won't be shown again."
            onDismiss={() => setCreated(null)}
            action={<CopyButton text={created} />}
          >
            <code className="secret">{created}</code>
          </Callout>
        )}
      </div>

      {keys === null ? (
        <div className="card__pad">
          <SkeletonRows rows={4} cols={6} />
        </div>
      ) : shown.length === 0 ? (
        <EmptyState compact icon={<IconKey size={18} />} title="No keys yet">
          Create one above, or get a demo key from the Playground.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Prefix</th>
                <th scope="col" className="num">
                  RPM
                </th>
                <th scope="col" className="num">
                  Monthly cap
                </th>
                <th scope="col">Routing</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((k) => (
                <tr key={k.id} className={k.active ? "" : "is-dim"}>
                  <td className="cell-strong">{k.name}</td>
                  <td>
                    <code className="code-pill">{k.key_prefix}…</code>
                  </td>
                  <td className="num">{k.rpm_limit ?? "∞"}</td>
                  <td className="num">{k.monthly_spend_cap_usd ? usd(k.monthly_spend_cap_usd) : "∞"}</td>
                  <td>{k.routing_quality}</td>
                  <td>
                    <Badge tone={k.active ? "ok" : "neutral"} dot>
                      {k.active ? "Active" : "Revoked"}
                    </Badge>
                  </td>
                  <td className="num">
                    <button
                      type="button"
                      className={`btn btn--sm ${k.active ? "btn--ghost-danger" : "btn--ghost"}`}
                      onClick={() => void toggle(k)}
                      disabled={toggling === k.id}
                      aria-label={`${k.active ? "Revoke" : "Reactivate"} key ${k.name}`}
                    >
                      {k.active ? "Revoke" : "Reactivate"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {visible.length > 10 && (
        <div className="card__foot">
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Show fewer" : `Show all ${visible.length} keys`}
          </button>
        </div>
      )}
    </Card>
  );
}

