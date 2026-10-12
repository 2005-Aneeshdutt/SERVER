import { useEffect, useMemo, useRef, useState } from "react";

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
  height?: number;
}

const PAD = { top: 10, right: 8, bottom: 22, left: 58 };

/** A max that splits into 4 round steps (0, 25, 50, 75, 100 rather than 0, 13, 25, 38, 50). */
function niceMax(v: number): number {
  if (v <= 0) return 4;
  const raw = v / 4;
  const exp = 10 ** Math.floor(Math.log10(raw));
  const n = raw / exp;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * exp;
  return step * 4;
}

export function TimeChart({ points, series, format, bucket, height = 190 }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const box = useRef<HTMLDivElement>(null);
  // Draw at the container's real pixel width so axis text renders at its true size.
  const [width, setWidth] = useState(360);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.max(240, Math.round(entry.contentRect.width)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const innerW = width - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;

  const max = useMemo(
    () => niceMax(Math.max(0, ...points.flatMap((p) => series.map((s) => p.values[s.key] ?? 0)))),
    [points, series],
  );
  const n = Math.max(points.length, 1);
  const slot = innerW / n;
  const x = (i: number) => PAD.left + slot * i + slot / 2;
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const label = (d: Date) =>
    bucket === "hour"
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const vx = ((e.clientX - box.left) / box.width) * width;
    const i = Math.floor((vx - PAD.left) / slot);
    setHover(i >= 0 && i < points.length ? i : null);
  };

  const bars = series.filter((s) => s.kind === "bar");
  const barW = Math.max(1, (slot * 0.7) / Math.max(bars.length, 1));
  const hovered = hover === null ? undefined : points[hover];

  return (
    <div className="chart" ref={box}>
      <svg
        ref={ref}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line
              className="chart-grid"
              x1={PAD.left}
              x2={width - PAD.right}
              y1={y(max * f)}
              y2={y(max * f)}
            />
            <text className="chart-axis" x={PAD.left - 8} y={y(max * f) + 4} textAnchor="end">
              {format(max * f)}
            </text>
          </g>
        ))}
        {points.length > 0 &&
          [0, Math.floor((points.length - 1) / 2), points.length - 1].map((i, n) => (
            <text
              key={i}
              className="chart-axis"
              x={n === 0 ? PAD.left : n === 2 ? width - PAD.right : x(i)}
              y={height - 6}
              textAnchor={n === 0 ? "start" : n === 2 ? "end" : "middle"}
            >
              {label(points[i]!.t)}
            </text>
          ))}
        {hover !== null && (
          <rect
            className="chart-hover"
            x={PAD.left + slot * hover}
            y={PAD.top}
            width={slot}
            height={innerH}
          />
        )}
        {bars.map((s, b) =>
          points.map((p, i) => {
            const v = p.values[s.key] ?? 0;
            return v > 0 ? (
              <rect
                key={`${s.key}-${i}`}
                x={x(i) - (barW * bars.length) / 2 + b * barW}
                y={y(v)}
                width={barW}
                height={PAD.top + innerH - y(v)}
                rx={Math.min(2, barW / 2)}
                fill={s.color}
              />
            ) : null;
          }),
        )}
        {series
          .filter((s) => s.kind === "line")
          .map((s) => (
            <polyline
              key={s.key}
              fill="none"
              stroke={s.color}
              strokeWidth={2}
              strokeLinejoin="round"
              points={points.map((p, i) => `${x(i)},${y(p.values[s.key] ?? 0)}`).join(" ")}
            />
          ))}
      </svg>
      <div className="chart-legend">
        {series.map((s) => (
          <span key={s.key}>
            <i style={{ background: s.color }} />
            {s.label}
            {hovered && <b>{format(hovered.values[s.key] ?? 0)}</b>}
          </span>
        ))}
        {hovered && <span className="muted">{hovered.t.toLocaleString()}</span>}
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
