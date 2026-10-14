import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { IconAlert, IconCheck, IconCopy, IconInfo, IconX } from "./icons";

/** Semantic tones shared by badges, dots, callouts and meters. */
export type Tone = "neutral" | "accent" | "ok" | "warn" | "bad" | "purple";

export function Badge({
  tone = "neutral",
  dot = false,
  children,
  title,
}: {
  tone?: Tone;
  dot?: boolean;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {dot && <span className="badge__dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function StatusDot({ tone, pulse = false, label }: { tone: Tone; pulse?: boolean; label?: string }) {
  return (
    <span
      className={`status-dot status-dot--${tone} ${pulse ? "status-dot--pulse" : ""}`}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}

export function Card({
  title,
  icon,
  description,
  actions,
  children,
  className = "",
  flush = false,
  id,
}: {
  title?: ReactNode;
  icon?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Let a table run edge to edge instead of inside the card padding. */
  flush?: boolean;
  id?: string;
}) {
  const headingId = useId();
  return (
    <section
      className={`card ${flush ? "card--flush" : ""} ${className}`}
      aria-labelledby={title ? headingId : undefined}
      id={id}
    >
      {(title || actions) && (
        <header className="card__head">
          <div className="card__titles">
            {title && (
              <h2 className="card__title" id={headingId}>
                {icon && <span className="card__icon">{icon}</span>}
                {title}
              </h2>
            )}
            {description && <p className="card__desc">{description}</p>}
          </div>
          {actions && <div className="card__actions">{actions}</div>}
        </header>
      )}
      <div className="card__body">{children}</div>
    </section>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  compact = false,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`empty ${compact ? "empty--compact" : ""}`}>
      {icon && <div className="empty__icon">{icon}</div>}
      <p className="empty__title">{title}</p>
      {children && <div className="empty__body">{children}</div>}
    </div>
  );
}

export function Skeleton({ width = "100%", height = 12, className = "" }: { width?: string | number; height?: number; className?: string }) {
  return <span className={`skeleton ${className}`} style={{ width, height }} aria-hidden="true" />;
}

export function SkeletonRows({ rows = 4, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="skeleton-rows" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, r) => (
        <div key={r} className="skeleton-row">
          {Array.from({ length: cols }, (_, c) => (
            <Skeleton key={c} width={c === 0 ? "70%" : "55%"} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function Callout({
  tone,
  title,
  children,
  action,
  onDismiss,
}: {
  tone: "info" | "warn" | "bad" | "ok";
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  onDismiss?: () => void;
}) {
  const Icon = tone === "info" ? IconInfo : tone === "ok" ? IconCheck : IconAlert;
  return (
    <div className={`callout callout--${tone}`} role={tone === "bad" ? "alert" : "status"}>
      <Icon className="icon callout__icon" />
      <div className="callout__text">
        {title && <strong className="callout__title">{title}</strong>}
        {children && <div>{children}</div>}
      </div>
      {action}
      {onDismiss && (
        <button type="button" className="icon-btn icon-btn--sm" onClick={onDismiss} aria-label="Dismiss">
          <IconX />
        </button>
      )}
    </div>
  );
}

/** A checkbox styled as a switch; keeps native semantics (role=switch) and keyboard handling. */
export function Switch({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
}) {
  const id = useId();
  return (
    <label className="switch" htmlFor={id}>
      <span className="switch__text">
        <span className="switch__label">{label}</span>
        {hint && <span className="switch__hint">{hint}</span>}
      </span>
      <input
        id={id}
        type="checkbox"
        role="switch"
        className="switch__input"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="switch__track" aria-hidden="true">
        <span className="switch__thumb" />
      </span>
    </label>
  );
}

export function Segmented<T extends string | number>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: ReactNode; title?: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          title={o.title}
          className={`segmented__btn ${o.value === value ? "is-active" : ""}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number>();
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked (insecure origin): the value is still selectable on screen */
    }
  }
  return (
    <button type="button" className="btn btn--secondary btn--sm" onClick={() => void copy()}>
      {copied ? <IconCheck /> : <IconCopy />}
      <span aria-live="polite">{copied ? "Copied" : label}</span>
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

/** Horizontal 0..1 meter with a tone, used for traffic share and agreement rates. */
export function Meter({ value, tone = "accent", label }: { value: number | null; tone?: Tone; label: string }) {
  const v = value === null ? 0 : Math.max(0, Math.min(1, value));
  return (
    <span
      className={`meter meter--${tone}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value === null ? undefined : Math.round(v * 100)}
    >
      <span className="meter__fill" style={{ width: `${v * 100}%` }} />
    </span>
  );
}
