import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { IconAlert, IconCheck, IconInfo, IconX } from "./icons";

export type ToastTone = "ok" | "bad" | "info";

interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  detail?: string;
  leaving: boolean;
}

type Push = (tone: ToastTone, title: string, detail?: string) => void;

const ToastContext = createContext<Push>(() => undefined);

/** `toast("ok", "Saved")` from anywhere under <ToastProvider>. */
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((all) => all.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    window.setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), 200);
  }, []);

  const push = useCallback<Push>(
    (tone, title, detail) => {
      const id = next.current++;
      setToasts((all) => [...all.slice(-3), { id, tone, title, detail, leaving: false }]);
      window.setTimeout(() => dismiss(id), tone === "bad" ? 6000 : 3800);
    },
    [dismiss],
  );

  const value = useMemo(() => push, [push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="region" aria-label="Notifications" aria-live="polite">
        {toasts.map((t) => {
          const Icon = t.tone === "ok" ? IconCheck : t.tone === "bad" ? IconAlert : IconInfo;
          return (
            <div key={t.id} className={`toast toast--${t.tone} ${t.leaving ? "is-leaving" : ""}`} role="status">
              <span className="toast__icon">
                <Icon size={14} />
              </span>
              <div className="toast__text">
                <strong>{t.title}</strong>
                {t.detail && <span>{t.detail}</span>}
              </div>
              <button type="button" className="icon-btn icon-btn--sm" onClick={() => dismiss(t.id)} aria-label="Dismiss notification">
                <IconX size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
