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
