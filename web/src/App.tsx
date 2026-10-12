import { useState } from "react";
import { getReadiness, type Readiness } from "./api";
import { Dashboard } from "./components/Dashboard";
import { Playground } from "./components/Playground";
import { usePolling, useStored } from "./hooks";

type Tab = "playground" | "dashboard";

export function App() {
  const [tab, setTab] = useStored("gw.tab", "playground");
  const [health, setHealth] = useState<Readiness | null | "down">(null);
  usePolling(() => getReadiness().then(setHealth, () => setHealth("down")), 10_000);

  const status =
    health === null
      ? { cls: "", text: "checking" }
      : health === "down"
        ? { cls: "bad", text: "unreachable" }
        : health.degraded
          ? { cls: "warn", text: "degraded" }
          : { cls: "ok", text: "healthy" };
  const detail =
    health && health !== "down"
      ? Object.entries(health.checks)
          .map(([k, v]) => `${k}: ${v}`)
          .join("\n")
      : "";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>
            ✦
          </span>
          LLM Gateway
        </div>
        <nav>
          {(["playground", "dashboard"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {t === "playground" ? "Playground" : "Dashboard"}
            </button>
          ))}
        </nav>
        <span className={`health ${status.cls}`} title={detail}>
          <i />
          {status.text}
        </span>
      </header>
      <main>{tab === "dashboard" ? <Dashboard /> : <Playground />}</main>
      <footer>
        OpenAI-compatible gateway · failover · rate &amp; spend limits · caching · smart routing
      </footer>
    </div>
  );
}
