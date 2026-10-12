export function usd(value: number | string | undefined | null): string {
  if (value === undefined || value === null || value === "") return "—";
  const n = Number(value);
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(6)}`;
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

export function ms(value: number | undefined | null): string {
  if (value === undefined || value === null) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 2)} s`;
  return `${Math.round(value)} ms`;
}

export function pct(value: number | undefined | null, digits = 1): string {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function count(value: number | undefined | null): string {
  if (value === undefined || value === null) return "—";
  return new Intl.NumberFormat().format(value);
}

export function time(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
