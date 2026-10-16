import { useEffect, useMemo, useState } from "react";
import { getReadiness, listModels, type ModelInfo, type Readiness } from "./api";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { Dashboard } from "./components/Dashboard";
import {
  IconChart,
  IconColumns,
  IconCpu,
  IconHome,
  IconMonitor,
  IconMoon,
  IconSearch,
  IconSun,
  IconTerminal,
  LogoMark,
} from "./components/icons";
import { Overview } from "./components/Overview";
import { Playground } from "./components/Playground";
import { ToastProvider } from "./components/toast";
import { Kbd, StatusDot, type Tone } from "./components/ui";
import { sendCommand, usePolling, useStored } from "./hooks";

type Tab = "overview" | "playground" | "dashboard";
type Theme = "system" | "light" | "dark";

const TABS: { id: Tab; label: string; Icon: typeof IconTerminal }[] = [
  { id: "overview", label: "Overview", Icon: IconHome },
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

const isTab = (v: string): v is Tab => v === "overview" || v === "playground" || v === "dashboard";

export function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}

function Shell() {
  const [storedTab, setTab] = useStored("gw.tab", "overview");
  const tab: Tab = isTab(storedTab) ? storedTab : "overview";
  const [theme, setTheme] = useTheme();
  const [health, setHealth] = useState<Readiness | null | "down">(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [models, setModels] = useState<ModelInfo[]>([]);
  usePolling(() => getReadiness().then(setHealth, () => setHealth("down")), 10_000);

  // Ctrl/Cmd+K toggles the palette from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Models for the palette come from the stored client key: prefetched on load, refreshed on open.
  useEffect(() => {
    let key = "";
    try {
      key = window.localStorage.getItem("gw.key") ?? "";
    } catch {
      /* storage unavailable */
    }
    if (key) listModels(key).then(setModels, () => setModels([]));
  }, [paletteOpen]);

  const commands = useMemo<Command[]>(() => {
    const persist = (k: string, v: string) => {
      try {
        window.localStorage.setItem(k, v);
      } catch {
        /* storage unavailable: the event below still reaches a mounted playground */
      }
    };
    return [
      ...TABS.map(({ id, label, Icon }) => ({
        id: `go-${id}`,
        group: "Navigate",
        label: `Go to ${label}`,
        icon: <Icon size={15} />,
        hint: tab === id ? "current" : undefined,
        run: () => setTab(id),
      })),
      {
        id: "compare-on",
        group: "Playground",
        label: "Compare two models",
        icon: <IconColumns size={15} />,
        keywords: "race side by side parallel",
        run: () => {
          persist("gw.mode", "compare");
          setTab("playground");
          sendCommand({ type: "compare", on: true });
        },
      },
      {
        id: "compare-off",
        group: "Playground",
        label: "Single request mode",
        icon: <IconTerminal size={15} />,
        run: () => {
          persist("gw.mode", "single");
          setTab("playground");
          sendCommand({ type: "compare", on: false });
        },
      },
      {
        id: "focus-prompt",
        group: "Playground",
        label: "Write a prompt",
        icon: <IconTerminal size={15} />,
        keywords: "focus textarea",
        run: () => {
          setTab("playground");
          window.setTimeout(() => sendCommand({ type: "focus-prompt" }), 50);
        },
      },
      ...models.map((m) => ({
        id: `model-${m.id}`,
        group: "Model",
        label: m.id === "auto" ? "auto — smart routing" : m.id,
        hint: m.owned_by,
        icon: <IconCpu size={15} />,
        keywords: "use select model",
        run: () => {
          persist("gw.model", m.id);
          setTab("playground");
          sendCommand({ type: "model", model: m.id });
        },
      })),
      ...THEMES.map(({ id, label, Icon }) => ({
        id: `theme-${id}`,
        group: "Theme",
        label,
        icon: <Icon size={15} />,
        hint: theme === id ? "current" : undefined,
        keywords: "appearance dark light mode",
        run: () => setTheme(id),
      })),
    ];
  }, [models, tab, theme, setTab, setTheme]);

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
          <button type="button" className="brand" onClick={() => setTab("overview")} aria-label="Switchyard home">
            <span className="brand__logo">
              <LogoMark size={18} />
            </span>
            <span className="brand__name">Switchyard</span>
          </button>
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
            <button
              type="button"
              className="search-btn"
              onClick={() => setPaletteOpen(true)}
              aria-label="Open command palette"
              aria-keyshortcuts="Control+K Meta+K"
            >
              <IconSearch size={14} />
              <span className="search-btn__text">Search…</span>
              <span className="search-btn__keys">
                <Kbd>Ctrl</Kbd>
                <Kbd>K</Kbd>
              </span>
            </button>
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
      <main id="main" className="main" key={tab}>
        {tab === "dashboard" ? (
          <Dashboard />
        ) : tab === "playground" ? (
          <Playground />
        ) : (
          <Overview onNavigate={setTab} health={health} />
        )}
      </main>
      <footer className="footer">
        OpenAI-compatible gateway <span aria-hidden="true">·</span> failover{" "}
        <span aria-hidden="true">·</span> rate &amp; spend limits <span aria-hidden="true">·</span> caching{" "}
        <span aria-hidden="true">·</span> smart routing <span aria-hidden="true">·</span> distillation
        <span className="footer__hint">
          Press <Kbd>Ctrl</Kbd> <Kbd>K</Kbd> for commands
        </span>
      </footer>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} commands={commands} />
    </div>
  );
}
