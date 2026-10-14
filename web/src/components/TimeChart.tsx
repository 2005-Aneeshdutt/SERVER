import { useEffect, useId, useMemo, useRef, useState } from "react";

export interface Series {
  key: string;
  label: string;
  color: string;
  kind: "bar" | "line";
}

export interface Point {
  t: Date;
  values: Record<string, number>;
}

interface Props {
  points: Point[];
  series: Series[];
  format: (v: number) => string;
  bucket: "minute" | "hour";
  /** Accessible name for the chart image. */
  label: string;
  height?: number;
}

const PAD = { top: 12, right: 8, bottom: 24, left: 56 };

/** A max that splits into 4 round steps (0, 25, 50, 75, 100 rather than 0, 13, 25, 38, 50). */
function niceMax(v: number): number {
  if (v <= 0) return 4;
  const raw = v / 4;
  const exp = 10 ** Math.floor(Math.log10(raw));
  const n = raw / exp;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * exp;
  return step * 4;
}

export function TimeChart({ points, series, format, bucket, label, height = 200 }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const gradientId = useId().replace(/:/g, "");
  // Draw at the container's real pixel width so axis text renders at its true size.
  const [width, setWidth] = useState(360);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.max(220, Math.round(entry.contentRect.width)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const innerW = width - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;

  const peak = useMemo(
    () => Math.max(0, ...points.flatMap((p) => series.map((s) => p.values[s.key] ?? 0))),
    [points, series],
  );
  const max = niceMax(peak);
  const n = Math.max(points.length, 1);
  const slot = innerW / n;
  const x = (i: number) => PAD.left + slot * i + slot / 2;
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const baseline = PAD.top + innerH;
  const tick = (d: Date) =>
    bucket === "hour" && points.length > 30
      ? d.toLocaleDateString([], { month: "short", day: "numeric" })
      : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    const vx = ((e.clientX - rect.left) / rect.width) * width;
    const i = Math.floor((vx - PAD.left) / slot);
    setHover(i >= 0 && i < points.length ? i : null);
  };

  const bars = series.filter((s) => s.kind === "bar");
  const lines = series.filter((s) => s.kind === "line");
  const barW = Math.max(1, (slot * 0.72) / Math.max(bars.length, 1));
  const hovered = hover === null ? undefined : points[hover];
  const tipLeft = hover === null ? 0 : (x(hover) / width) * 100;

  return (
    <div className="chart" ref={box}>
      <div className="chart__plot">
        <svg
          ref={ref}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`${label}. Peak ${format(peak)}.`}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            {lines.map((s) => (
              <linearGradient key={s.key} id={`${gradientId}-${s.key}`} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={0.18} />
                <stop offset="100%" stopColor={s.color} stopOpacity={0} />
              </linearGradient>
            ))}
          </defs>
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <g key={f}>
              <line
                className={f === 0 ? "chart__base" : "chart__grid"}
                x1={PAD.left}
                x2={width - PAD.right}
                y1={y(max * f)}
                y2={y(max * f)}
              />
              <text className="chart__axis" x={PAD.left - 8} y={y(max * f) + 4} textAnchor="end">
                {format(max * f)}
              </text>
            </g>
          ))}
          {points.length > 0 &&
            [0, Math.floor((points.length - 1) / 2), points.length - 1].map((i, k) => (
              <text
                key={`${i}-${k}`}
                className="chart__axis"
                x={k === 0 ? PAD.left : k === 2 ? width - PAD.right : x(i)}
                y={height - 6}
                textAnchor={k === 0 ? "start" : k === 2 ? "end" : "middle"}
              >
                {tick(points[i]!.t)}
              </text>
            ))}
          {hover !== null && (
            <rect className="chart__hover" x={PAD.left + slot * hover} y={PAD.top} width={slot} height={innerH} />
          )}
          {bars.map((s, b) =>
            points.map((p, i) => {
              const v = p.values[s.key] ?? 0;
              return v > 0 ? (
                <rect
                  key={`${s.key}-${i}`}
                  x={x(i) - (barW * bars.length) / 2 + b * barW}
                  y={y(v)}
                  width={Math.max(1, barW - (bars.length > 1 ? 0.5 : 0))}
                  height={Math.max(1, baseline - y(v))}
                  rx={Math.min(2, barW / 2)}
                  fill={s.color}
                  opacity={hover === null || hover === i ? 1 : 0.55}
                />
              ) : null;
            }),
          )}
          {lines.map((s) => {
            const coords = points.map((p, i) => `${x(i)},${y(p.values[s.key] ?? 0)}`);
            if (coords.length === 0) return null;
            return (
              <g key={s.key}>
                <polygon
                  fill={`url(#${gradientId}-${s.key})`}
                  points={`${x(0)},${baseline} ${coords.join(" ")} ${x(points.length - 1)},${baseline}`}
                />
                <polyline
                  fill="none"
                  stroke={s.color}
                  strokeWidth={1.75}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  points={coords.join(" ")}
                />
                {hovered && (
                  <circle
                    cx={x(hover!)}
                    cy={y(hovered.values[s.key] ?? 0)}
                    r={3.5}
                    fill="var(--surface)"
                    stroke={s.color}
                    strokeWidth={2}
                  />
                )}
              </g>
            );
          })}
        </svg>
        {peak === 0 && <div className="chart__empty">No data in this range</div>}
        {hovered && (
          <div
            className={`chart__tip ${tipLeft > 60 ? "is-left" : ""}`}
            style={{ left: `${tipLeft}%` }}
            aria-hidden="true"
          >
            <span className="chart__tip-time">{hovered.t.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>
            {series.map((s) => (
              <span key={s.key} className="chart__tip-row">
                <i style={{ background: s.color }} />
                {s.label}
                <b>{format(hovered.values[s.key] ?? 0)}</b>
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="chart__legend">
        {series.map((s) => (
          <span key={s.key}>
            <i style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Fill missing buckets with zeros so gaps show as quiet periods, not interpolated lines. */
export function fillBuckets(
  rows: { bucket: string; values: Record<string, number> }[],
  since: Date,
  until: Date,
  bucket: "minute" | "hour",
): Point[] {
  const step = bucket === "minute" ? 60_000 : 3_600_000;
  const byTime = new Map(rows.map((r) => [new Date(r.bucket).getTime(), r.values]));
  const start = Math.floor(since.getTime() / step) * step;
  const points: Point[] = [];
  for (let t = start; t <= until.getTime(); t += step) {
    points.push({ t: new Date(t), values: byTime.get(t) ?? {} });
  }
  return points;
}
