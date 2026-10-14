import { useEffect, useState } from "react";
import { getReadiness, type Readiness } from "./api";
import { Dashboard } from "./components/Dashboard";
import { IconChart, IconMonitor, IconMoon, IconSun, IconTerminal, LogoMark } from "./components/icons";
import { Playground } from "./components/Playground";
import { StatusDot, type Tone } from "./components/ui";
import { usePolling, useStored } from "./hooks";

type Tab = "playground" | "dashboard";
type Theme = "system" | "light" | "dark";

const TABS: { id: Tab; label: string; Icon: typeof IconTerminal }[] = [
  { id: "playground", label: "Playground", Icon: IconTerminal },
  { id: "dashboard", label: "Dashboard", Icon: IconChart },
];

const THEMES: { id: Theme; label: string; Icon: typeof IconSun }[] = [
  { id: "system", label: "System theme", Icon: IconMonitor },
  { id: "light", label: "Light theme", Icon: IconSun },
  { id: "dark", label: "Dark theme", Icon: IconMoon },
];

/** "system" removes the attribute so the prefers-color-scheme media query decides. */
function useTheme(): [Theme, (t: Theme) => void] {
  const [stored, setStored] = useStored("gw.theme", "system");
  const theme: Theme = stored === "light" || stored === "dark" ? stored : "system";
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
  }, [theme]);
  return [theme, setStored];
}

export function App() {
  const [storedTab, setTab] = useStored("gw.tab", "playground");
  const tab: Tab = storedTab === "dashboard" ? "dashboard" : "playground";
  const [theme, setTheme] = useTheme();
  const [health, setHealth] = useState<Readiness | null | "down">(null);
  usePolling(() => getReadiness().then(setHealth, () => setHealth("down")), 10_000);

  const status: { tone: Tone; text: string } =
    health === null
      ? { tone: "neutral", text: "Checking" }
      : health === "down"
        ? { tone: "bad", text: "Unreachable" }
        : health.degraded
          ? { tone: "warn", text: "Degraded" }
          : { tone: "ok", text: "Operational" };
  const detail =
    health && health !== "down"
      ? Object.entries(health.checks)
          .map(([k, v]) => `${k}: ${v}`)
          .join("\n")
      : "";

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar__inner">
          <div className="brand">
            <span className="brand__logo">
              <LogoMark size={18} />
            </span>
            <span className="brand__name">LLM Gateway</span>
          </div>
          <nav className="tabs" aria-label="Sections">
            {TABS.map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                className={`tabs__btn ${tab === id ? "is-active" : ""}`}
                aria-current={tab === id ? "page" : undefined}
                onClick={() => setTab(id)}
              >
                <Icon />
                <span>{label}</span>
              </button>
            ))}
          </nav>
          <div className="topbar__right">
            <span
              className={`health health--${status.tone}`}
              title={detail || undefined}
              role="status"
              aria-label={`Gateway status: ${status.text}`}
            >
              <StatusDot tone={status.tone} pulse={status.tone === "ok"} />
              <span className="health__text">{status.text}</span>
            </span>
            <div className="theme-switch" role="radiogroup" aria-label="Color theme">
              {THEMES.map(({ id, label, Icon }) => (
                <button
                  key={id}
                  type="button"
                  role="radio"
                  aria-checked={theme === id}
                  aria-label={label}
                  title={label}
                  className={`theme-switch__btn ${theme === id ? "is-active" : ""}`}
                  onClick={() => setTheme(id)}
                >
                  <Icon size={14} />
                </button>
              ))}
            </div>
          </div>
        </div>
      </header>
      <main id="main" className="main">
        {tab === "dashboard" ? <Dashboard /> : <Playground />}
      </main>
      <footer className="footer">
        OpenAI-compatible gateway <span aria-hidden="true">·</span> failover{" "}
        <span aria-hidden="true">·</span> rate &amp; spend limits <span aria-hidden="true">·</span> caching{" "}
        <span aria-hidden="true">·</span> smart routing <span aria-hidden="true">·</span> distillation
      </footer>
    </div>
  );
}
