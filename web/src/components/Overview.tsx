import { useCallback, useMemo, useState } from "react";
import {
  ApiError,
  getProviders,
  getRecent,
  getTimeseries,
  type ProviderStatus,
  type Readiness,
  type RecentRequest,
  type Timeseries,
} from "../api";
import { ago, ms, usd } from "../format";
import { usePolling, useStored } from "../hooks";
import { ArchDiagram, type DiagramProvider } from "./ArchDiagram";
import {
  IconActivity,
  IconArrowRight,
  IconChart,
  IconClock,
  IconCoins,
  IconDatabase,
  IconFlask,
  IconGauge,
  IconLock,
  IconRoute,
  IconShield,
  IconShuffle,
  IconTerminal,
} from "./icons";
import { Badge, Card, CopyButton, Sparkline, StatusDot, Ticker, type Tone } from "./ui";

type Tab = "playground" | "dashboard";

const FALLBACK_PROVIDERS: DiagramProvider[] = ["anthropic", "gemini", "groq", "ollama", "openai", "openrouter"].map(
  (name) => ({ name, state: "closed" }),
);

const FEATURES: { icon: React.ReactNode; title: string; body: string; tab: Tab; cta: string }[] = [
  {
    icon: <IconRoute />,
    title: "Smart routing",
    body: "model: \"auto\" embeds the prompt (bge-small, CPU) and a classifier picks cheap or strong. No LLM call on the hot path.",
    tab: "playground",
    cta: "Route a prompt",
  },
  {
    icon: <IconShuffle />,
    title: "Failover & circuit breakers",
    body: "Retries with jittered backoff, fallback chains, and a breaker whose state lives in Redis so every replica agrees.",
    tab: "dashboard",
    cta: "Take a provider down",
  },
  {
    icon: <IconShield />,
    title: "Rate & spend limits",
    body: "Atomic Lua scripts in Redis. Spend is reserved before the call and settled after, so parallel requests never overspend.",
    tab: "dashboard",
    cta: "Manage keys",
  },
  {
    icon: <IconDatabase />,
    title: "Exact cache, no stampedes",
    body: "Identical deterministic requests are served from Redis for $0, and 100 concurrent misses coalesce into one upstream call.",
    tab: "playground",
    cta: "Get a cache hit",
  },
  {
    icon: <IconGauge />,
    title: "Measured, not guessed",
    body: "Server-Timing on every response, Prometheus metrics, and request logs written off the hot path to Postgres.",
    tab: "playground",
    cta: "See a request trace",
  },
  {
    icon: <IconFlask />,
    title: "Distillation",
    body: "Repeated tasks are captured through Kafka, distilled into small models, and rolled out shadow → canary → full with auto-rollback.",
    tab: "dashboard",
    cta: "View rollout",
  },
];

export function Overview({ onNavigate, health }: { onNavigate: (t: Tab) => void; health: Readiness | null | "down" }) {
  const [token, setToken] = useStored("gw.admin", "", "session");
  const [series, setSeries] = useState<Timeseries | null>(null);
  const [recent, setRecent] = useState<RecentRequest[] | null>(null);
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);

  const signOutOn401 = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) setToken("");
    },
    [setToken],
  );
  usePolling(
    () => getTimeseries(token, 24, "hour").then(setSeries, signOutOn401),
    15_000,
    Boolean(token),
  );
  usePolling(
    () =>
      Promise.all([getRecent(token, 12), getProviders(token)]).then(([r, p]) => {
        setRecent(r.rows);
        setProviders(p);
      }, signOutOn401),
    3_000,
    Boolean(token),
  );

  const diagramProviders = providers ?? FALLBACK_PROVIDERS;
  const live = Boolean(token) && recent !== null;

  return (
    <div className="overview">
      <section className="hero">
        <div className="hero__glow" aria-hidden="true" />
        <div className="hero__copy">
          <span className="hero__eyebrow">
            <StatusDot tone={health && health !== "down" ? (health.degraded ? "warn" : "ok") : "neutral"} pulse />
            Switchyard · OpenAI-compatible AI gateway
          </span>
          <h1 className="hero__title">
            One endpoint for every model.
            <span className="hero__title-accent"> Fast, cheap and never down.</span>
          </h1>
          <p className="hero__sub">
            Point any OpenAI SDK at the gateway. It handles auth, rate and spend limits, caching, failover and smart
            routing, and logs cost and latency for every request.
          </p>
          <div className="hero__ctas">
            <button type="button" className="btn btn--primary btn--lg btn--glow" onClick={() => onNavigate("playground")}>
              <IconTerminal /> Open the playground
            </button>
            <button type="button" className="btn btn--secondary btn--lg" onClick={() => onNavigate("dashboard")}>
              <IconChart /> View the dashboard
            </button>
          </div>
          <Snippet />
        </div>
        <div className="hero__side">
          {token ? <HeroStats series={series} /> : <SignIn onToken={setToken} />}
          <SystemStatus health={health} />
        </div>
      </section>

      <Card
        title="How a request flows"
        icon={<IconActivity />}
        description={
          live
            ? "Each dot is a real request from the last few seconds, travelling the path the gateway took."
            : "Every request passes the same pipeline. Sign in to watch live traffic move through it."
        }
        actions={
          live ? (
            <Badge tone="ok" dot>
              Live
            </Badge>
          ) : (
            <Badge tone="neutral">Illustrative</Badge>
          )
        }
        className="overview__flow"
      >
        <div className="flow">
          <ArchDiagram providers={diagramProviders} recent={recent} live={live} />
          {live && <LiveFeed rows={recent ?? []} />}
        </div>
      </Card>

      <section aria-labelledby="features-title">
        <h2 id="features-title" className="section-title">
          What it does
        </h2>
        <div className="features">
          {FEATURES.map((f) => (
            <button key={f.title} type="button" className="feature" onClick={() => onNavigate(f.tab)}>
              <span className="feature__icon">{f.icon}</span>
              <span className="feature__title">{f.title}</span>
              <span className="feature__body">{f.body}</span>
              <span className="feature__cta">
                {f.cta} <IconArrowRight size={14} />
              </span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function Snippet() {
  const origin = typeof window === "undefined" ? "http://localhost:8000" : window.location.origin;
  const code = `from openai import OpenAI

client = OpenAI(base_url="${origin}/v1", api_key="gw-…")
client.chat.completions.create(model="auto", messages=[...])`;
  return (
    <figure className="snippet">
      <figcaption className="snippet__bar">
        <span className="snippet__dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="snippet__file">quickstart.py</span>
        <CopyButton text={code} />
      </figcaption>
      <pre className="snippet__code">
        <code>
          <span className="tok-k">from</span> openai <span className="tok-k">import</span> OpenAI{"\n\n"}
          client = OpenAI(base_url=<span className="tok-s">"{origin}/v1"</span>, api_key=
          <span className="tok-s">"gw-…"</span>){"\n"}
          client.chat.completions.create(model=<span className="tok-s">"auto"</span>, messages=[...])
        </code>
      </pre>
    </figure>
  );
}

function HeroStats({ series }: { series: Timeseries | null }) {
  const t = series?.totals;
  const rows = series?.rows ?? [];
  const reqs = rows.map((r) => r.requests);
  const cost = rows.map((r) => Number(r.cost_usd));
  const p50 = rows.map((r) => r.p50_latency_ms ?? 0);
  const hits = rows.map((r) => r.cache_hits);
  const stats: { label: string; icon: React.ReactNode; value: number | null; format: (v: number) => string; spark: number[]; tone: Tone }[] = [
    {
      label: "Requests · 24h",
      icon: <IconActivity size={14} />,
      value: t ? (t.requests ?? 0) : null,
      format: (v) => Math.round(v).toLocaleString(),
      spark: reqs,
      tone: "accent",
    },
    {
      label: "Latency p50",
      icon: <IconClock size={14} />,
      value: t ? (t.p50_latency_ms ?? null) : null,
      format: (v) => ms(v),
      spark: p50,
      tone: "warn",
    },
    {
      label: "Spend · 24h",
      icon: <IconCoins size={14} />,
      value: t ? Number(t.cost_usd ?? 0) : null,
      format: (v) => usd(v),
      spark: cost,
      tone: "ok",
    },
    {
      label: "Cache hits",
      icon: <IconDatabase size={14} />,
      value: t ? (t.cache_hits ?? 0) : null,
      format: (v) => Math.round(v).toLocaleString(),
      spark: hits,
      tone: "purple",
    },
  ];
  return (
    <div className="hero-stats" aria-busy={!series}>
      {stats.map((s) => (
        <div key={s.label} className="hero-stat">
          <span className="hero-stat__label">
            {s.icon}
            {s.label}
          </span>
          <strong className="hero-stat__value">
            {series ? <Ticker value={s.value} format={s.format} /> : <span className="skeleton" style={{ width: 90, height: 26 }} />}
          </strong>
          <Sparkline values={s.spark} tone={s.tone} label={`${s.label} trend over 24 hours`} />
        </div>
      ))}
    </div>
  );
}

function SignIn({ onToken }: { onToken: (t: string) => void }) {
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
    <form className="hero-signin" onSubmit={(e) => void submit(e)}>
      <span className="hero-signin__icon">
        <IconLock size={18} />
      </span>
      <div>
        <p className="hero-signin__title">See live traffic</p>
        <p className="hero-signin__sub">Sign in with the admin token for live stats and the request flow.</p>
      </div>
      <label className="sr-only" htmlFor="overview-token">
        Admin token
      </label>
      <div className="hero-signin__row">
        <input
          id="overview-token"
          className="input input--mono"
          type="password"
          placeholder="Admin token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
        />
        <button className="btn btn--primary" type="submit" disabled={!value || busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Sign in
        </button>
      </div>
      {error && <p className="hero-signin__error">{error}</p>}
    </form>
  );
}

function SystemStatus({ health }: { health: Readiness | null | "down" }) {
  const checks = health && health !== "down" ? Object.entries(health.checks) : [];
  return (
    <div className="sys">
      <span className="sys__title">System status</span>
      {health === null && <span className="sys__item">checking…</span>}
      {health === "down" && (
        <span className="sys__item">
          <StatusDot tone="bad" /> gateway unreachable
        </span>
      )}
      {checks.map(([name, state]) => (
        <span key={name} className="sys__item">
          <StatusDot tone={state === "ok" ? "ok" : "warn"} />
          {name.replace("_", " ")}
        </span>
      ))}
    </div>
  );
}

function LiveFeed({ rows }: { rows: RecentRequest[] }) {
  const top = useMemo(() => rows.slice(0, 7), [rows]);
  return (
    <ol className="feed" aria-label="Most recent requests">
      {top.length === 0 && <li className="feed__empty">Waiting for traffic…</li>}
      {top.map((r) => (
        <li key={r.id} className="feed__item">
          <StatusDot tone={r.status !== "ok" ? "bad" : r.cache_hit ? "ok" : "accent"} />
          <div className="feed__main">
            <span className="feed__alias">{r.alias}</span>
            <span className="feed__sub">
              {r.provider} · {ago(r.created_at)}
            </span>
          </div>
          <span className="feed__lat">
            {r.status !== "ok" ? (r.error_type ?? "error") : r.cache_hit ? "cache" : ms(r.latency_ms)}
          </span>
        </li>
      ))}
    </ol>
  );
}
