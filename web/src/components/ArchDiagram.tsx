import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { RecentRequest } from "../api";
import { useReducedMotion } from "../hooks";

/**
 * Hand-built architecture diagram. Two layouts (wide and narrow) share one edge/packet engine:
 * every edge is a cubic Bézier between node anchors, and a request "packet" travels the
 * concatenated path client → auth → limits → cache → router → breaker → provider.
 * When `live` is true, packets are real rows from /admin/requests as they appear.
 */

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

type Orient = "h" | "v";

const MODULES = [
  { id: "auth", label: "Auth", sub: "sha256 keys" },
  { id: "limits", label: "Limits", sub: "Lua · Redis" },
  { id: "cache", label: "Cache", sub: "coalesced" },
  { id: "router", label: "Router", sub: "bge + LR" },
  { id: "breaker", label: "Breaker", sub: "shared state" },
] as const;

export interface DiagramProvider {
  name: string;
  state: string;
}

interface Layout {
  w: number;
  h: number;
  orient: Orient;
  client: Rect;
  frame: Rect;
  modules: Rect[];
  infra: Rect[];
  providers: Rect[];
  kafka: Rect;
  distill: Rect;
  llama: Rect;
}

function wideLayout(n: number): Layout {
  const top = 36;
  const span = 290;
  const ph = 32;
  const gap = n > 1 ? Math.min(14, (span - n * ph) / (n - 1)) : 0;
  const used = n * ph + (n - 1) * gap;
  const py = top + (span - used) / 2;
  return {
    w: 1000,
    h: 440,
    orient: "h",
    client: { x: 16, y: 129, w: 132, h: 64 },
    frame: { x: 196, y: 36, w: 484, h: 280 },
    modules: MODULES.map((_, i) => ({ x: 216 + i * 90, y: 131, w: 76, h: 60 })),
    infra: [
      { x: 216, y: 248, w: 214, h: 40 },
      { x: 442, y: 248, w: 220, h: 40 },
    ],
    providers: Array.from({ length: n }, (_, i) => ({ x: 776, y: py + i * (ph + gap), w: 200, h: ph })),
    kafka: { x: 236, y: 368, w: 160, h: 56 },
    distill: { x: 476, y: 368, w: 184, h: 56 },
    llama: { x: 776, y: 368, w: 200, h: 56 },
  };
}

function narrowLayout(n: number): Layout {
  const rows = Math.ceil(n / 2);
  const provTop = 470;
  const provBottom = provTop + rows * 42;
  return {
    w: 360,
    h: provBottom + 130,
    orient: "v",
    client: { x: 100, y: 8, w: 160, h: 56 },
    frame: { x: 8, y: 96, w: 344, h: 330 },
    modules: MODULES.map((_, i) => ({ x: 40, y: 146 + i * 44, w: 280, h: 34 })),
    infra: [
      { x: 28, y: 374, w: 146, h: 36 },
      { x: 186, y: 374, w: 146, h: 36 },
    ],
    providers: Array.from({ length: n }, (_, i) => ({
      x: i % 2 === 0 ? 8 : 184,
      y: provTop + Math.floor(i / 2) * 42,
      w: 168,
      h: 32,
    })),
    kafka: { x: 8, y: provBottom + 46, w: 108, h: 56 },
    distill: { x: 126, y: provBottom + 46, w: 108, h: 56 },
    llama: { x: 244, y: provBottom + 46, w: 108, h: 56 },
  };
}

const cx = (r: Rect) => r.x + r.w / 2;
const cy = (r: Rect) => r.y + r.h / 2;

/** Bézier from a's exit side to b's entry side. */
function curve(a: Rect, b: Rect, orient: Orient): string {
  if (orient === "h") {
    const x1 = a.x + a.w;
    const y1 = cy(a);
    const x2 = b.x;
    const y2 = cy(b);
    const d = Math.max(24, (x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + d} ${y1}, ${x2 - d} ${y2}, ${x2} ${y2}`;
  }
  const x1 = cx(a);
  const y1 = a.y + a.h;
  const x2 = cx(b);
  const y2 = b.y;
  const d = Math.max(16, (y2 - y1) / 2);
  return `M ${x1} ${y1} C ${x1} ${y1 + d}, ${x2} ${y2 - d}, ${x2} ${y2}`;
}

/** Join contiguous segments into one path (drop each later segment's leading move-to). */
function join(parts: string[]): string {
  return parts.map((p, i) => (i === 0 ? p : p.replace(/^M\s+[\d.-]+\s+[\d.-]+\s*/, ""))).join(" ");
}

function packetPath(l: Layout, providerIndex: number | null, stopAt: number): string {
  const parts = [curve(l.client, l.modules[0]!, l.orient)];
  for (let i = 0; i < stopAt; i++) parts.push(curve(l.modules[i]!, l.modules[i + 1]!, l.orient));
  if (providerIndex !== null) {
    const target = providerIndex < 0 ? l.llama : l.providers[providerIndex]!;
    parts.push(curve(l.modules[4]!, target, l.orient));
  }
  return join(parts);
}

const STATE_TONE: Record<string, string> = {
  closed: "ok",
  half_open: "warn",
  open: "bad",
  forced_down: "bad",
  not_configured: "off",
};

interface Packet {
  id: string;
  path: string;
  tone: "accent" | "ok" | "bad";
  dur: number;
  target: string;
}

function PacketDot({ packet, onDone }: { packet: Packet; onDone: (p: Packet) => void }) {
  const anim = useRef<SVGAnimateMotionElement>(null);
  useLayoutEffect(() => {
    // Dynamically inserted SMIL animations must be started explicitly.
    anim.current?.beginElement();
    const t = window.setTimeout(() => onDone(packet), packet.dur * 1000 + 50);
    return () => window.clearTimeout(t);
  }, []);
  return (
    <g className={`packet packet--${packet.tone}`}>
      <circle r="9" className="packet__halo" />
      <circle r="4" className="packet__core" />
      <animateMotion
        ref={anim}
        begin="indefinite"
        dur={`${packet.dur}s`}
        path={packet.path}
        fill="freeze"
        calcMode="spline"
        keyTimes="0;1"
        keySplines="0.45 0 0.25 1"
      />
    </g>
  );
}

export function ArchDiagram({
  providers,
  recent,
  live,
}: {
  providers: DiagramProvider[];
  recent: RecentRequest[] | null;
  live: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  const reduced = useReducedMotion();
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => e && setNarrow(e.contentRect.width < 620));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const shown = useMemo(() => providers.filter((p) => p.name !== "llamacpp").slice(0, 8), [providers]);
  const hasLlama = providers.some((p) => p.name === "llamacpp");
  const llamaState = providers.find((p) => p.name === "llamacpp")?.state ?? "closed";
  const layout = useMemo(() => (narrow ? narrowLayout(shown.length) : wideLayout(shown.length)), [narrow, shown.length]);
  const l = layout;

  const [packets, setPackets] = useState<Packet[]>([]);
  const [flash, setFlash] = useState<Record<string, number>>({});
  const seen = useRef<Set<string> | null>(null);

  const spawn = (id: string, providerName: string, tone: Packet["tone"], cacheHit: boolean) => {
    const found = shown.findIndex((p) => p.name === providerName);
    // -1 targets the llama.cpp node; null stops at the breaker (unknown provider).
    const idx = providerName === "llamacpp" ? -1 : found >= 0 ? found : null;
    const path = cacheHit ? packetPath(l, null, 2) : packetPath(l, idx, 4);
    const dur = cacheHit ? 1.1 : 1.9;
    const target = cacheHit ? "cache" : providerName;
    setPackets((all) => [...all.slice(-14), { id: `${id}-${Date.now()}`, path, tone, dur, target }]);
  };

  // Live mode: animate each request row we have not seen before.
  useEffect(() => {
    if (!live || !recent || reduced) return;
    if (seen.current === null) {
      seen.current = new Set(recent.map((r) => r.id));
      recent.slice(0, 4).forEach((r, i) =>
        window.setTimeout(() => spawn(r.id, r.provider, r.status !== "ok" ? "bad" : r.cache_hit ? "ok" : "accent", r.cache_hit), i * 260),
      );
      return;
    }
    const fresh = recent.filter((r) => !seen.current!.has(r.id)).slice(0, 6);
    fresh.forEach((r) => seen.current!.add(r.id));
    fresh
      .reverse()
      .forEach((r, i) =>
        window.setTimeout(
          () => spawn(r.id, r.provider, r.status !== "ok" ? "bad" : r.cache_hit ? "ok" : "accent", r.cache_hit),
          i * 220,
        ),
      );
  }, [recent, live, reduced, layout]);

  // Illustrative mode (signed out): a gentle, clearly-labelled sample flow.
  useEffect(() => {
    if (live || reduced) return;
    const healthy = shown.filter((p) => p.state !== "not_configured");
    const pool = healthy.length ? healthy : shown;
    let n = 0;
    const id = window.setInterval(() => {
      const p = pool[n % Math.max(pool.length, 1)];
      n += 1;
      if (p) spawn(`demo-${n}`, p.name, n % 5 === 0 ? "ok" : "accent", n % 5 === 0);
    }, 1500);
    return () => window.clearInterval(id);
  }, [live, reduced, layout, shown]);

  const done = (p: Packet) => {
    setPackets((all) => all.filter((x) => x.id !== p.id));
    setFlash((f) => ({ ...f, [p.target]: Date.now() }));
  };

  // Clear flashes after their animation.
  useEffect(() => {
    if (!Object.keys(flash).length) return;
    const t = window.setTimeout(() => {
      const cutoff = Date.now() - 700;
      setFlash((f) => Object.fromEntries(Object.entries(f).filter(([, v]) => v > cutoff)));
    }, 750);
    return () => window.clearTimeout(t);
  }, [flash]);

  const isFlash = (name: string) => flash[name] !== undefined;

  const providerEdges = shown.map((_, i) => curve(l.modules[4]!, l.providers[i]!, l.orient));
  const moduleEdges = MODULES.slice(1).map((_, i) => curve(l.modules[i]!, l.modules[i + 1]!, l.orient));
  const captureFrom: Rect =
    l.orient === "h"
      ? { x: l.frame.x + 120, y: l.frame.y + l.frame.h - 1, w: 0, h: 1 }
      : { x: l.frame.x, y: l.frame.y + 40, w: 1, h: 0 };
  const captureEdge =
    l.orient === "h"
      ? `M ${captureFrom.x} ${captureFrom.y} C ${captureFrom.x} ${l.kafka.y - 20}, ${cx(l.kafka)} ${l.kafka.y - 30}, ${cx(l.kafka)} ${l.kafka.y}`
      : `M ${l.frame.x + 18} ${l.frame.y + l.frame.h} L ${l.frame.x + 18} ${l.kafka.y - 18} Q ${l.frame.x + 18} ${l.kafka.y - 4}, ${cx(l.kafka)} ${l.kafka.y}`;
  const flowEdge = (a: Rect, b: Rect) =>
    l.orient === "h"
      ? curve(a, b, "h")
      : `M ${a.x + a.w} ${cy(a)} L ${b.x} ${cy(b)}`;
  const serveEdge =
    l.orient === "h"
      ? `M ${l.modules[4]!.x + l.modules[4]!.w} ${cy(l.modules[4]!)} C ${l.frame.x + l.frame.w + 50} ${cy(l.modules[4]!) + 40}, ${l.llama.x - 70} ${cy(l.llama)}, ${l.llama.x} ${cy(l.llama)}`
      : `M ${l.frame.x + l.frame.w - 18} ${l.frame.y + l.frame.h} L ${l.frame.x + l.frame.w - 18} ${l.llama.y - 18} Q ${l.frame.x + l.frame.w - 18} ${l.llama.y - 4}, ${cx(l.llama)} ${l.llama.y}`;

  const label = `Architecture: your app calls the gateway, which runs auth, limits, cache, router and circuit breaker before calling one of ${shown.length} providers. Requests are captured to Kafka for distillation; distilled models are served by llama.cpp.`;

  return (
    <div className={`arch ${narrow ? "arch--narrow" : ""} ${reduced ? "" : "arch--motion"}`} ref={box}>
      <svg viewBox={`0 0 ${l.w} ${l.h}`} role="img" aria-label={label}>
        <defs>
          <pattern id="arch-dots" width="16" height="16" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1" className="arch__dot" />
          </pattern>
          <linearGradient id="arch-frame" x1="0" x2="1" y1="0" y2="1">
            <stop offset="0%" className="arch__frame-a" />
            <stop offset="100%" className="arch__frame-b" />
          </linearGradient>
        </defs>
        <rect x="0" y="0" width={l.w} height={l.h} fill="url(#arch-dots)" />

        {/* edges */}
        <path d={curve(l.client, l.modules[0]!, l.orient)} className="edge edge--main" />
        {moduleEdges.map((d, i) => (
          <path key={i} d={d} className="edge edge--main" />
        ))}
        {providerEdges.map((d, i) => (
          <path key={shown[i]!.name} d={d} className={`edge edge--provider edge--${STATE_TONE[shown[i]!.state] ?? "off"}`} />
        ))}
        <path d={captureEdge} className="edge edge--async" />
        <path d={flowEdge(l.kafka, l.distill)} className="edge edge--async" />
        <path d={flowEdge(l.distill, l.llama)} className="edge edge--async" />
        <path d={serveEdge} className="edge edge--serve" />

        {/* gateway frame */}
        <rect x={l.frame.x} y={l.frame.y} width={l.frame.w} height={l.frame.h} rx="16" className="arch__frame" />
        <text x={l.frame.x + 18} y={l.frame.y + 28} className="arch__frame-title">
          LLM Gateway
        </text>
        <text x={l.frame.x + 18} y={l.frame.y + 46} className="arch__frame-sub">
          FastAPI · async · stateless replicas
        </text>

        {MODULES.map((m, i) => {
          const r = l.modules[i]!;
          return (
            <g key={m.id} className={`node node--module ${isFlash(m.id) ? "is-flash" : ""}`}>
              <rect x={r.x} y={r.y} width={r.w} height={r.h} rx="10" />
              {l.orient === "h" ? (
                <>
                  <text x={cx(r)} y={r.y + 24} textAnchor="middle" className="node__label">
                    {m.label}
                  </text>
                  <text x={cx(r)} y={r.y + 41} textAnchor="middle" className="node__sub">
                    {m.sub}
                  </text>
                </>
              ) : (
                <>
                  <text x={r.x + 14} y={cy(r) + 4} className="node__label">
                    {m.label}
                  </text>
                  <text x={r.x + r.w - 14} y={cy(r) + 4} textAnchor="end" className="node__sub">
                    {m.sub}
                  </text>
                </>
              )}
            </g>
          );
        })}

        {[
          { label: "Redis", sub: "limits · cache · breaker" },
          { label: "Postgres", sub: "primary + read replica" },
        ].map((inf, i) => {
          const r = l.infra[i]!;
          return (
            <g key={inf.label} className="node node--infra">
              <rect x={r.x} y={r.y} width={r.w} height={r.h} rx={r.h / 2} />
              <text x={r.x + 16} y={cy(r) + 4} className="node__label node__label--sm">
                {inf.label}
              </text>
              <text x={r.x + r.w - 14} y={cy(r) + 4} textAnchor="end" className="node__sub">
                {l.orient === "h" ? inf.sub : inf.sub.split(" ")[0]}
              </text>
            </g>
          );
        })}

        {/* client */}
        <g className="node node--client">
          <rect x={l.client.x} y={l.client.y} width={l.client.w} height={l.client.h} rx="12" />
          <text x={cx(l.client)} y={l.client.y + 26} textAnchor="middle" className="node__label">
            Your app
          </text>
          <text x={cx(l.client)} y={l.client.y + 44} textAnchor="middle" className="node__sub">
            OpenAI SDK · /v1
          </text>
        </g>

        {/* providers */}
        {shown.map((p, i) => {
          const r = l.providers[i]!;
          const tone = STATE_TONE[p.state] ?? "off";
          return (
            <g key={p.name} className={`node node--provider node--${tone} ${isFlash(p.name) ? "is-flash" : ""}`}>
              <rect x={r.x} y={r.y} width={r.w} height={r.h} rx="8" />
              <circle cx={r.x + 16} cy={cy(r)} r="4" className="node__status" />
              <text x={r.x + 30} y={cy(r) + 4} className="node__label node__label--sm">
                {p.name}
              </text>
              {l.orient === "h" && (
                <text x={r.x + r.w - 12} y={cy(r) + 4} textAnchor="end" className="node__sub">
                  {p.state === "closed" ? "healthy" : p.state.replace("_", " ")}
                </text>
              )}
            </g>
          );
        })}

        {/* distillation lane */}
        {[
          { r: l.kafka, label: "Kafka", sub: "capture · async" },
          { r: l.distill, label: "Distill", sub: l.orient === "h" ? "discover · train · eval gate" : "train · eval" },
          { r: l.llama, label: "llama.cpp", sub: hasLlama ? `distilled · ${llamaState === "closed" ? "up" : llamaState}` : "distilled models" },
        ].map(({ r, label: nodeLabel, sub }) => (
          <g
            key={nodeLabel}
            className={`node node--lane ${nodeLabel === "llama.cpp" && isFlash("llamacpp") ? "is-flash" : ""}`}
          >
            <rect x={r.x} y={r.y} width={r.w} height={r.h} rx="12" />
            <text x={cx(r)} y={r.y + 24} textAnchor="middle" className="node__label">
              {nodeLabel}
            </text>
            <text x={cx(r)} y={r.y + 41} textAnchor="middle" className="node__sub">
              {sub}
            </text>
          </g>
        ))}

        {packets.map((p) => (
          <PacketDot key={p.id} packet={p} onDone={done} />
        ))}
      </svg>
      <div className="arch__legend">
        <span>
          <i className="arch__key arch__key--accent" /> request
        </span>
        <span>
          <i className="arch__key arch__key--ok" /> cache hit
        </span>
        <span>
          <i className="arch__key arch__key--bad" /> error
        </span>
        <span>
          <i className="arch__key arch__key--async" /> async capture
        </span>
        <span className="arch__mode">
          {reduced ? "Animation off (reduced motion)" : live ? "Live: real requests as they arrive" : "Illustrative flow · sign in for live traffic"}
        </span>
      </div>
    </div>
  );
}
