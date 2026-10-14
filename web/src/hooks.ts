import { useCallback, useEffect, useRef, useState } from "react";

/** State persisted in localStorage (or sessionStorage for secrets that shouldn't outlive the tab). */
export function useStored(
  key: string,
  initial: string,
  storage: "local" | "session" = "local",
): [string, (v: string) => void] {
  const store = () => (storage === "local" ? window.localStorage : window.sessionStorage);
  const [value, setValue] = useState<string>(() => {
    try {
      return store().getItem(key) ?? initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: string) => {
      setValue(v);
      try {
        if (v) store().setItem(key, v);
        else store().removeItem(key);
      } catch {
        /* storage unavailable (private mode): keep the value in memory only */
      }
    },
    [key],
  );
  return [value, set];
}

/** Run `fn` now and every `ms` milliseconds while `enabled`. */
export function usePolling(fn: () => void | Promise<void>, ms: number, enabled = true): void {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!enabled) return;
    void saved.current();
    const id = window.setInterval(() => void saved.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, enabled]);
}

/** True while the user asks the OS for reduced motion. */
export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );
  useEffect(() => {
    const mql = window.matchMedia(query);
    const on = () => setMatches(mql.matches);
    on();
    mql.addEventListener("change", on);
    return () => mql.removeEventListener("change", on);
  }, [query]);
  return matches;
}

/** Ease a number towards `target` (ease-out cubic). Snaps immediately under reduced motion. */
export function useAnimatedNumber(target: number | null, duration = 700): number | null {
  const reduced = useReducedMotion();
  const [value, setValue] = useState<number | null>(target);
  const from = useRef<number | null>(target);
  useEffect(() => {
    if (target === null || reduced) {
      from.current = target;
      setValue(target);
      return;
    }
    const start = from.current ?? 0; // first real value counts up from zero
    if (start === target) return;
    const t0 = performance.now();
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / duration);
      const eased = 1 - (1 - k) ** 3;
      const v = start + (target - start) * eased;
      from.current = v;
      setValue(v);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, reduced, duration]);
  return value;
}

/** App-wide commands sent from the command palette to whichever view handles them. */
export type AppCommand =
  | { type: "model"; model: string }
  | { type: "compare"; on: boolean }
  | { type: "focus-prompt" };

const COMMAND_EVENT = "gw:command";

export function sendCommand(cmd: AppCommand): void {
  window.dispatchEvent(new CustomEvent<AppCommand>(COMMAND_EVENT, { detail: cmd }));
}

export function useCommand(handler: (cmd: AppCommand) => void): void {
  const saved = useRef(handler);
  saved.current = handler;
  useEffect(() => {
    const on = (e: Event) => saved.current((e as CustomEvent<AppCommand>).detail);
    window.addEventListener(COMMAND_EVENT, on);
    return () => window.removeEventListener(COMMAND_EVENT, on);
  }, []);
}
