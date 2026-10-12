import { useCallback, useMemo, useState } from "react";
import {
  ApiError,
  createKey,
  getProviders,
  getRecent,
  getTimeseries,
  getUsage,
  listKeys,
  setKeyActive,
  setOutage,
  type ApiKey,
  type ProviderStatus,
  type RecentRequest,
  type Timeseries,
  type UsageRow,
} from "../api";
import { ago, count, ms, pct, time, usd } from "../format";
import { usePolling, useStored } from "../hooks";
import { fillBuckets, TimeChart } from "./TimeChart";

const RANGES = [
  { label: "Last hour", hours: 1, bucket: "minute" },
  { label: "Last 24 hours", hours: 24, bucket: "hour" },
  { label: "Last 7 days", hours: 168, bucket: "hour" },
] as const;

export function Dashboard() {
  const [token, setToken] = useStored("gw.admin", "", "session");
  if (!token) return <AdminGate onToken={setToken} />;
  return <Console token={token} onSignOut={() => setToken("")} />;
}

function AdminGate({ onToken }: { onToken: (t: string) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    try {
      await getProviders(value);
      onToken(value);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }
  return (
    <form className="panel gate" onSubmit={(e) => void submit(e)}>
      <h2>Admin sign-in</h2>
      <p className="muted">The dashboard uses the gateway's ADMIN_TOKEN. It's kept for this tab only.</p>
      <input
        type="password"
        placeholder="Admin token"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        autoFocus
      />
      <button className="primary" type="submit" disabled={!value}>
        Open dashboard
      </button>
      {error && <p className="notice">{error}</p>}
    </form>
  );
}

function Console({ token, onSignOut }: { token: string; onSignOut: () => void }) {
  const [rangeIndex, setRangeIndex] = useState(1);
  const range = RANGES[rangeIndex]!;
  const [series, setSeries] = useState<Timeseries | null>(null);
  const [usage, setUsage] = useState<UsageRow[]>([]);
  const [recent, setRecent] = useState<RecentRequest[]>([]);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [error, setError] = useState<string | null>(null);

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
        const [r, p] = await Promise.all([getRecent(token), getProviders(token)]);
        setRecent(r.rows);
        setProviders(p);
      }),
    [guard, token],
  );
  usePolling(refreshSlow, 15_000);
  usePolling(refreshFast, 3_000);

  const keyNames = useMemo(() => new Map(keys.map((k) => [k.id, k.name])), [keys]);

  return (
    <div className="dashboard">
      <div className="toolbar">
        <div className="segmented">
          {RANGES.map((r, i) => (
            <button
              key={r.label}
              className={i === rangeIndex ? "active" : ""}
              onClick={() => setRangeIndex(i)}
            >
              {r.label}
            </button>
          ))}
        </div>
        <div className="toolbar-right">
          {series && (
            <span className="muted" title="Dashboard reads go to the streaming read replica">
              data from {series.source}
              {series.replica_lag_s !== null && ` · lag ≤ ${series.replica_lag_s.toFixed(1)}s`}
            </span>
          )}
          <button className="ghost" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </div>
      {error && <p className="notice">{error}</p>}

      <Kpis series={series} />
      <Charts series={series} />

      <div className="grid-2">
        <ProvidersPanel
          providers={providers}
          onToggle={(name, down) =>
            guard(async () => {
              await setOutage(token, name, down);
              setProviders(await getProviders(token));
            })
          }
        />
        <UsagePanel rows={usage} keyNames={keyNames} />
      </div>

      <RecentPanel rows={recent} />
      <KeysPanel
        keys={keys}
        onCreate={async (k) => {
          let raw = "";
          await guard(async () => {
            raw = (await createKey(token, k)).key;
            setKeys(await listKeys(token));
          });
          return raw;
        }}
        onToggle={(id, active) =>
          guard(async () => {
            await setKeyActive(token, id, active);
            setKeys(await listKeys(token));
          })
        }
      />
    </div>
  );
}

function Kpis({ series }: { series: Timeseries | null }) {
  const t = series?.totals ?? {};
  const requests = t.requests ?? 0;
  const items = [
    { label: "Requests", value: count(t.requests) },
    { label: "Spend", value: usd(t.cost_usd) },
    { label: "Cache hit rate", value: requests ? pct((t.cache_hits ?? 0) / requests) : "—" },
    { label: "Error rate", value: requests ? pct((t.errors ?? 0) / requests) : "—" },
    { label: "Failovers", value: count(t.fallbacks) },
    { label: "Latency p50 / p95", value: `${ms(t.p50_latency_ms)} / ${ms(t.p95_latency_ms)}` },
  ];
  return (
    <div className="kpis">
      {items.map((k) => (
        <div key={k.label} className="kpi">
          <span>{k.label}</span>
          <strong>{k.value}</strong>
        </div>
      ))}
    </div>
  );
}

function Charts({ series }: { series: Timeseries | null }) {
  if (!series) return <div className="panel chart-placeholder">Loading…</div>;
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
  const points = fillBuckets(rows, new Date(series.since), new Date(series.until), series.bucket);
  return (
    <div className="grid-3">
      <div className="panel">
        <h3>Requests</h3>
        <TimeChart
          points={points}
          bucket={series.bucket}
          format={(v) => String(Math.round(v))}
          series={[
            { key: "ok", label: "Succeeded", color: "var(--accent)", kind: "bar" },
            { key: "errors", label: "Errors", color: "var(--bad)", kind: "bar" },
          ]}
        />
      </div>
      <div className="panel">
        <h3>Spend</h3>
        <TimeChart
          points={points}
          bucket={series.bucket}
          format={(v) => usd(v)}
          series={[{ key: "cost", label: "Cost", color: "var(--ok)", kind: "bar" }]}
        />
      </div>
      <div className="panel">
        <h3>Latency</h3>
        <TimeChart
          points={points}
          bucket={series.bucket}
          format={(v) => ms(v)}
          series={[
            { key: "p50", label: "p50", color: "var(--accent)", kind: "line" },
            { key: "p95", label: "p95", color: "var(--warn)", kind: "line" },
          ]}
        />
      </div>
    </div>
  );
}

const STATE_LABEL: Record<string, [string, string]> = {
  closed: ["healthy", "badge-green"],
  half_open: ["recovering", "badge-amber"],
  open: ["circuit open", "badge-red"],
  forced_down: ["taken down", "badge-red"],
  not_configured: ["no API key", "badge-gray"],
};

function ProvidersPanel({
  providers,
  onToggle,
}: {
  providers: ProviderStatus[];
  onToggle: (name: string, down: boolean) => void;
}) {
  return (
    <div className="panel">
      <h3>Providers</h3>
      <p className="muted small">
        Taking a provider down affects all traffic: requests fail over along their fallback
        chains, exactly as in a real outage.
      </p>
      <ul className="providers">
        {providers.map((p) => {
          const [label, cls] = STATE_LABEL[p.state] ?? [p.state, "badge-gray"];
          const down = p.state === "forced_down";
          return (
            <li key={p.name}>
              <div>
                <strong>{p.name}</strong>
                <small className="muted">{p.aliases.join(", ")}</small>
              </div>
              <span className={`badge ${cls}`}>{label}</span>
              <button
                className={down ? "secondary" : "danger"}
                disabled={!p.configured}
                onClick={() => onToggle(p.name, !down)}
              >
                {down ? "Restore" : "Take down"}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function UsagePanel({ rows, keyNames }: { rows: UsageRow[]; keyNames: Map<string, string> }) {
  return (
    <div className="panel">
      <h3>Usage by key and model</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Key</th>
              <th>Model</th>
              <th className="num">Requests</th>
              <th className="num">Cache hits</th>
              <th className="num">p95</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No traffic in this range yet.
                </td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={`${r.key_id}-${r.alias}`}>
                <td>{(r.key_id && keyNames.get(r.key_id)) ?? "—"}</td>
                <td>{r.alias}</td>
                <td className="num">{count(r.requests)}</td>
                <td className="num">{count(r.cache_hits)}</td>
                <td className="num">{ms(r.p95_latency_ms)}</td>
                <td className="num">{usd(r.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RecentPanel({ rows }: { rows: RecentRequest[] }) {
  return (
    <div className="panel">
      <h3>
        Live requests <span className="live-dot" title="Refreshes every 3 seconds" />
      </h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Key</th>
              <th>Requested</th>
              <th>Served by</th>
              <th>Route</th>
              <th>Status</th>
              <th className="num">Latency</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  Waiting for requests. Send one from the Playground.
                </td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                <td title={new Date(r.created_at).toLocaleString()}>
                  {time(r.created_at)} <small className="muted">{ago(r.created_at)}</small>
                </td>
                <td>{r.key_name ?? "—"}</td>
                <td>{r.alias}</td>
                <td>
                  {r.provider} <small className="muted">{r.upstream_model}</small>
                  {r.fallback_count > 0 && <span className="badge badge-amber">failover</span>}
                  {r.cache_hit && <span className="badge badge-green">cache</span>}
                </td>
                <td>
                  {r.route ? (
                    <span className={`badge ${r.route === "cheap" ? "badge-green" : "badge-purple"}`}>
                      {r.route}
                      {r.route_probability !== null && ` ${r.route_probability.toFixed(2)}`}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <span className={`badge ${r.status === "ok" ? "badge-green" : "badge-red"}`}>
                    {r.status === "ok" ? "ok" : (r.error_type ?? r.status)}
                  </span>
                </td>
                <td className="num">{ms(r.latency_ms)}</td>
                <td className="num">{usd(r.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function KeysPanel({
  keys,
  onCreate,
  onToggle,
}: {
  keys: ApiKey[];
  onCreate: (k: {
    name: string;
    rpm_limit?: number;
    monthly_spend_cap_usd?: string;
    routing_quality: string;
  }) => Promise<string>;
  onToggle: (id: string, active: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [rpm, setRpm] = useState("60");
  const [cap, setCap] = useState("1");
  const [quality, setQuality] = useState("balanced");
  const [created, setCreated] = useState<string | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const visible = keys
    .filter((k) => showRevoked || k.active)
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
  const shown = showAll ? visible : visible.slice(0, 10);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
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
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>API keys</h3>
        <label className="toggle small">
          <input
            type="checkbox"
            checked={showRevoked}
            onChange={(e) => setShowRevoked(e.target.checked)}
          />
          Show revoked
        </label>
      </div>
      <form className="key-form" onSubmit={(e) => void submit(e)}>
        <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} required />
        <input placeholder="Requests / min" value={rpm} onChange={(e) => setRpm(e.target.value)} />
        <input placeholder="Monthly cap ($)" value={cap} onChange={(e) => setCap(e.target.value)} />
        <select value={quality} onChange={(e) => setQuality(e.target.value)}>
          <option value="low">auto: save most</option>
          <option value="balanced">auto: balanced</option>
          <option value="high">auto: best quality</option>
        </select>
        <button className="primary" type="submit">
          Create key
        </button>
      </form>
      {created && (
        <div className="created-key">
          <span>Copy this key now; it won't be shown again:</span>
          <code>{created}</code>
          <button className="ghost" onClick={() => void navigator.clipboard?.writeText(created)}>
            Copy
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th className="num">RPM</th>
              <th className="num">Monthly cap</th>
              <th>Routing</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td>
                  <code>{k.key_prefix}…</code>
                </td>
                <td className="num">{k.rpm_limit ?? "∞"}</td>
                <td className="num">{k.monthly_spend_cap_usd ? usd(k.monthly_spend_cap_usd) : "∞"}</td>
                <td>{k.routing_quality}</td>
                <td>
                  <button
                    className={`link ${k.active ? "" : "muted"}`}
                    onClick={() => onToggle(k.id, !k.active)}
                    title={k.active ? "Revoke" : "Re-activate"}
                  >
                    {k.active ? "active" : "revoked"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {visible.length > 10 && (
        <button className="ghost show-more" onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show fewer" : `Show all ${visible.length} keys`}
        </button>
      )}
    </div>
  );
}
