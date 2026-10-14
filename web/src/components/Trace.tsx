import { useEffect, useState, type ReactNode } from "react";
import type { ChatResult } from "../api";
import { ms } from "../format";
import { IconCheck, IconDatabase, IconFlask, IconRoute, IconServer, IconShield, IconX, IconZap } from "./icons";

/**
 * A request trace built only from what the gateway already tells us:
 * Server-Timing (gateway;dur, upstream;dur), X-Gateway-* headers, and client-side timings
 * (time to headers, time to first token, total). Nothing here is invented: when a value is
 * missing the stage says so instead of guessing.
 */

export type LivePhase = "waiting" | "streaming";

type StageState = "done" | "hit" | "skip" | "warn" | "bad" | "active" | "pending";

interface Stage {
  id: string;
  label: string;
  detail: string;
  state: StageState;
  icon: ReactNode;
}

const ICONS = {
  auth: <IconShield size={14} />,
  cache: <IconDatabase size={14} />,
  route: <IconRoute size={14} />,
  provider: <IconServer size={14} />,
  stream: <IconZap size={14} />,
};

function finishedStages(r: ChatResult, streamed: boolean): Stage[] {
  const hit = r.cache === "HIT";
  const rejected = !r.ok && r.attempts === 0 && !hit;
  const auth: Stage = {
    id: "auth",
    label: "Auth & limits",
    detail: rejected ? `rejected · ${r.status}` : "key · rate · spend",
    state: rejected ? "bad" : "done",
    icon: ICONS.auth,
  };
  const cache: Stage = {
    id: "cache",
    label: "Cache",
    detail: rejected ? "not reached" : hit ? "hit · $0" : (r.cache?.toLowerCase() ?? "skipped"),
    state: rejected ? "pending" : hit ? "hit" : r.cache === "MISS" ? "done" : "skip",
    icon: ICONS.cache,
  };
  const route: Stage = {
    id: "route",
    label: "Router",
    detail: r.route
      ? `${r.route}${r.routeProbability !== undefined ? ` · p ${r.routeProbability.toFixed(2)}` : ""}`
      : "fixed model",
    state: rejected ? "pending" : r.route ? "done" : "skip",
    icon: ICONS.route,
  };
  const provider: Stage = {
    id: "provider",
    label: "Provider",
    detail: hit
      ? "not called"
      : r.fallbacks > 0
        ? `${r.provider ?? "?"} · ${r.fallbacks} fallback${r.fallbacks > 1 ? "s" : ""}`
        : (r.provider ?? (rejected ? "not reached" : "—")),
    state: rejected ? "pending" : hit ? "skip" : !r.ok ? "bad" : r.fallbacks > 0 ? "warn" : "done",
    icon: ICONS.provider,
  };
  const stream: Stage = {
    id: "stream",
    label: streamed ? "Stream" : "Response",
    detail: !r.ok
      ? "error"
      : r.completionTokens !== undefined
        ? `${r.completionTokens} tokens`
        : hit && streamed
          ? "replayed"
          : "done",
    state: !r.ok ? (rejected ? "pending" : "bad") : "done",
    icon: ICONS.stream,
  };
  return [auth, cache, route, provider, stream];
}

function liveStages(phase: LivePhase, streamed: boolean, routed: boolean): Stage[] {
  const waiting = phase === "waiting";
  return [
    { id: "auth", label: "Auth & limits", detail: "checked", state: "done", icon: ICONS.auth },
    { id: "cache", label: "Cache", detail: "looked up", state: "done", icon: ICONS.cache },
    {
      id: "route",
      label: "Router",
      detail: routed ? "deciding" : "fixed model",
      state: routed ? "done" : "skip",
      icon: ICONS.route,
    },
    {
      id: "provider",
      label: "Provider",
      detail: waiting ? "awaiting first token" : "answering",
      state: waiting ? "active" : "done",
      icon: ICONS.provider,
    },
    {
      id: "stream",
      label: streamed ? "Stream" : "Response",
      detail: waiting ? "pending" : "receiving",
      state: waiting ? "pending" : "active",
      icon: ICONS.stream,
    },
  ];
}

function StageIcon({ stage }: { stage: Stage }) {
  if (stage.state === "done" || stage.state === "hit") return <IconCheck size={13} />;
  if (stage.state === "bad") return <IconX size={13} />;
  return <>{stage.icon}</>;
}

export function TraceRail({
  result,
  live,
  streamed,
  routed,
}: {
  result?: ChatResult;
  live?: LivePhase;
  streamed: boolean;
  routed: boolean;
}) {
  const stages = live ? liveStages(live, streamed, routed) : result ? finishedStages(result, streamed) : [];
  return (
    <ol className={`rail ${live ? "is-live" : "is-done"}`} aria-label="Request stages">
      {stages.map((s, i) => (
        <li
          key={s.id}
          className={`rail__stage rail__stage--${s.state}`}
          style={{ animationDelay: `${i * 90}ms` }}
          aria-label={`${s.label}: ${s.detail}`}
        >
          <span className="rail__node" aria-hidden="true">
            <StageIcon stage={s} />
          </span>
          <span className="rail__label">{s.label}</span>
          <span className="rail__detail">{s.detail}</span>
        </li>
      ))}
    </ol>
  );
}

/** Live clock for an in-flight request. */
export function useElapsed(running: boolean): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!running) return;
    const t0 = performance.now();
    setNow(0);
    const id = window.setInterval(() => setNow(performance.now() - t0), 47);
    return () => window.clearInterval(id);
  }, [running]);
  return now;
}

interface Span {
  id: string;
  label: string;
  sub: string;
  start: number;
  dur: number;
  tone: "gateway" | "route" | "provider" | "network" | "stream" | "cache" | "distill";
  dashed?: boolean;
}

/** Turn one finished request into positioned spans on a 0..total time axis. */
export function spans(r: ChatResult, streamed: boolean): Span[] {
  const total = Math.max(r.totalMs, 1);
  const gw = r.overheadMs ?? 0;
  const up = r.upstreamMs ?? 0;
  const routeMs = r.route ? Math.min(r.routeMs ?? 0, gw) : 0;
  const serverDone = gw + up;
  const headers = Math.min(Math.max(r.headersMs ?? serverDone, serverDone), total);
  const hit = r.cache === "HIT";
  const out: Span[] = [];
  const pre = Math.max(gw - routeMs, 0);
  out.push({
    id: "gw",
    label: "Gateway",
    sub: hit ? "auth · limits · cache hit" : "auth · limits · cache lookup",
    start: 0,
    dur: pre,
    tone: hit ? "cache" : "gateway",
  });
  if (routeMs > 0) {
    out.push({ id: "route", label: "Router", sub: `classifier → ${r.route}`, start: pre, dur: routeMs, tone: "route" });
  }
  if (!hit && (up > 0 || r.attempts > 0)) {
    const attempts = `${r.attempts} attempt${r.attempts === 1 ? "" : "s"}`;
    out.push({
      id: "up",
      label: r.provider ?? "Provider",
      sub: r.fallbacks > 0 ? `${attempts} · ${r.fallbacks} fallback` : `${attempts} · until first chunk`,
      start: gw,
      dur: up,
      tone: "provider",
    });
  }
  if (headers - serverDone > 1) {
    out.push({
      id: "net",
      label: "Network",
      sub: "gateway → browser",
      start: serverDone,
      dur: headers - serverDone,
      tone: "network",
    });
  }
  out.push({
    id: "body",
    label: streamed ? "Stream" : "Body",
    sub:
      r.completionTokens && total - headers > 50 && streamed && !hit
        ? `${Math.round(r.completionTokens / ((total - headers) / 1000))} tok/s`
        : streamed
          ? hit
            ? "replayed from cache"
            : "chunks"
          : "JSON",
    start: headers,
    dur: total - headers,
    tone: "stream",
  });
  if (r.distill?.outcome === "shadow") {
    out.push({
      id: "distill",
      label: "Distilled shadow",
      sub: `${r.distill.task} · async, off the hot path`,
      start: headers,
      dur: total - headers,
      tone: "distill",
      dashed: true,
    });
  }
  return out;
}

export function Waterfall({ result, streamed }: { result: ChatResult; streamed: boolean }) {
  const total = Math.max(result.totalMs, 1);
  const rows = spans(result, streamed);
  const pctOf = (v: number) => `${Math.max(0, Math.min(100, (v / total) * 100))}%`;
  const ttft = result.ttftMs;
  return (
    <div className="waterfall" role="table" aria-label="Request timing waterfall">
      <div className="waterfall__axis" role="row" aria-hidden="true">
        <span className="waterfall__name" />
        <span className="waterfall__track">
          <span style={{ left: "0%" }}>0</span>
          <span style={{ left: "50%" }}>{ms(total / 2)}</span>
          <span style={{ left: "100%" }}>{ms(total)}</span>
        </span>
        <span className="waterfall__dur" />
      </div>
      {rows.map((s, i) => (
        <div key={s.id} className="waterfall__row" role="row">
          <span className="waterfall__name" role="rowheader">
            <span className={`waterfall__swatch waterfall__swatch--${s.tone}`} aria-hidden="true" />
            <span className="waterfall__label">{s.label}</span>
            <span className="waterfall__sub">{s.sub}</span>
          </span>
          <span className="waterfall__track" role="cell">
            {ttft !== undefined && <span className="waterfall__marker" style={{ left: pctOf(ttft) }} aria-hidden="true" />}
            <span
              className={`waterfall__bar waterfall__bar--${s.tone} ${s.dashed ? "is-dashed" : ""}`}
              style={{ left: pctOf(s.start), width: `max(3px, ${pctOf(s.dur)})`, animationDelay: `${i * 70}ms` }}
              title={`${s.label}: ${ms(s.dur)} (starts at ${ms(s.start)})`}
            />
          </span>
          <span className="waterfall__dur" role="cell">
            {s.dashed ? "async" : ms(s.dur)}
          </span>
        </div>
      ))}
      <p className="waterfall__note">
        {ttft !== undefined && (
          <>
            <span className="waterfall__ttft-key" aria-hidden="true" /> first token at {ms(ttft)} ·{" "}
          </>
        )}
        Gateway and provider times come from <code>Server-Timing</code>; network and stream are measured in
        your browser.
      </p>
    </div>
  );
}

/** One-line proportional bar used in compare mode. */
export function StackedBar({ result, streamed }: { result: ChatResult; streamed: boolean }) {
  const total = Math.max(result.totalMs, 1);
  const rows = spans(result, streamed).filter((s) => !s.dashed);
  return (
    <div className="stacked" role="img" aria-label={rows.map((s) => `${s.label} ${ms(s.dur)}`).join(", ")}>
      {rows.map((s) => (
        <span
          key={s.id}
          className={`stacked__seg waterfall__bar--${s.tone}`}
          style={{ flexGrow: Math.max(s.dur / total, 0.012) }}
          title={`${s.label}: ${ms(s.dur)}`}
        />
      ))}
    </div>
  );
}

/** Headline numbers above the waterfall. */
export function TraceStats({ result }: { result: ChatResult }) {
  const share = result.overheadMs !== undefined && result.totalMs > 0 ? result.overheadMs / result.totalMs : undefined;
  const items: { label: string; value: string; sub?: string; accent?: boolean }[] = [
    { label: "Total", value: ms(result.totalMs) },
    { label: "First token", value: ms(result.ttftMs), sub: result.ttftMs === undefined ? "not streamed" : undefined },
    {
      label: "Gateway overhead",
      value: ms(result.overheadMs),
      sub: share !== undefined ? `${(share * 100).toFixed(share < 0.01 ? 2 : 1)}% of total` : undefined,
      accent: true,
    },
    { label: "Provider", value: result.cache === "HIT" ? "skipped" : ms(result.upstreamMs), sub: "until first chunk" },
  ];
  return (
    <dl className="trace-stats">
      {items.map((it) => (
        <div key={it.label} className={`trace-stats__item ${it.accent ? "is-accent" : ""}`}>
          <dt>{it.label}</dt>
          <dd>
            <strong>{it.value}</strong>
            {it.sub && <span>{it.sub}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function DistillChip({ outcome, task }: { outcome: string; task: string }) {
  return (
    <span className={`distill-chip distill-chip--${outcome}`} title={`X-Gateway-Distill: ${task}; ${outcome}`}>
      <IconFlask size={12} />
      {task} · {outcome}
    </span>
  );
}
