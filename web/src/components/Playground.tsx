import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  chat,
  createDemoKey,
  listModels,
  type ChatResult,
  type Message,
  type ModelInfo,
} from "../api";
import { ms, usd } from "../format";
import { useStored } from "../hooks";
import { Markdown } from "./Markdown";

const EXAMPLES = [
  { label: "Quick fact", prompt: "What is the capital of Australia? Answer in one sentence." },
  {
    label: "Code",
    prompt: "Write a Python function that returns the n-th Fibonacci number in O(log n) time.",
  },
  { label: "Reasoning", prompt: "Prove that the square root of 2 is irrational." },
  { label: "Creative", prompt: "Write a haiku about load balancers." },
];

interface Run extends ChatResult {
  id: number;
  requested: string; // model the user picked (e.g. "auto"); ChatResult.model is upstream
  prompt: string;
  messages: Message[]; // what was sent, so "Continue" can resume the conversation
}

/** A visible system message: tells the model its budget so it plans a complete answer. */
function conciseInstruction(maxTokens: number): Message {
  // ~0.75 words per token, halved again for headroom (and for reasoning models' thinking).
  const words = Math.max(40, Math.floor(maxTokens * 0.5));
  return {
    role: "system",
    content:
      `Your whole reply must fit in about ${words} words. Plan for that budget: be concise, ` +
      "prefer short explanations and compact code, and always end with a complete sentence.",
  };
}

const CONTINUE: Message = {
  role: "user",
  content: "Continue exactly where you stopped. Do not repeat anything you already wrote.",
};

export function Playground() {
  const [key, setKey] = useStored("gw.key", "");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useStored("gw.model", "auto");
  const [prompt, setPrompt] = useState(EXAMPLES[0]!.prompt);
  const [stream, setStream] = useState(true);
  const [deterministic, setDeterministic] = useState(true);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [concise, setConcise] = useState(true);
  const [down, setDown] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [live, setLive] = useState<{ text: string; reasoning: string }>({ text: "", reasoning: "" });
  const [runs, setRuns] = useState<Run[]>([]);

  useEffect(() => {
    if (!key) return;
    listModels(key)
      .then((list) => {
        setModels(list);
        if (!list.some((m) => m.id === model) && list[0]) setModel(list[0].id);
        setNotice(null);
      })
      .catch((e: unknown) => setNotice(e instanceof ApiError ? e.message : String(e)));
  }, [key]);

  const providers = useMemo(
    () => [...new Set(models.map((m) => m.owned_by).filter((p) => p !== "gateway"))].sort(),
    [models],
  );
  const last = runs[0];

  async function getDemoKey() {
    try {
      const demo = await createDemoKey();
      setKey(demo.key);
      setNotice(
        `Demo key created: ${demo.rpm_limit} requests/min, ${usd(demo.monthly_spend_cap_usd)} spend cap.`,
      );
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function send() {
    if (!key || !prompt.trim() || busy) return;
    const messages: Message[] = [
      ...(concise ? [conciseInstruction(maxTokens)] : []),
      { role: "user", content: prompt },
    ];
    setBusy(true);
    setLive({ text: "", reasoning: "" });
    try {
      const result = await chat(
        {
          key,
          model,
          messages,
          stream,
          deterministic,
          maxTokens,
          simulateOutage: down,
          reasoningEffort: concise ? "low" : undefined,
        },
        (text, reasoning) => setLive({ text, reasoning }),
      );
      setRuns((prev) =>
        [{ ...result, id: Date.now(), requested: model, prompt, messages }, ...prev].slice(0, 8),
      );
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  }

  /** Resume an answer that hit max_tokens: send it back as context and append the rest. */
  async function continueRun(run: Run) {
    if (!key || busy) return;
    const messages: Message[] = [
      ...run.messages,
      { role: "assistant", content: run.text },
      CONTINUE,
    ];
    setBusy(true);
    setLive({ text: run.text, reasoning: run.reasoning });
    try {
      const more = await chat(
        { key, model: run.requested, messages, stream, deterministic, maxTokens, simulateOutage: down },
        (text, reasoning) => setLive({ text: run.text + text, reasoning: run.reasoning + reasoning }),
      );
      const merged: Run = {
        ...run,
        ok: more.ok,
        status: more.status,
        error: more.error,
        text: run.text + more.text,
        reasoning: run.reasoning + more.reasoning,
        finishReason: more.finishReason,
        totalMs: run.totalMs + more.totalMs,
        costUsd: (run.costUsd ?? 0) + (more.costUsd ?? 0),
        completionTokens: (run.completionTokens ?? 0) + (more.completionTokens ?? 0),
        attempts: run.attempts + more.attempts,
        messages: [...messages, { role: "assistant", content: more.text }],
      };
      setRuns((prev) => prev.map((r) => (r.id === run.id ? merged : r)));
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  }

  const toggleDown = (p: string) =>
    setDown((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]));

  return (
    <div className="playground">
      <section className="panel compose">
        <div className="field">
          <label htmlFor="key">API key</label>
          <div className="row">
            <input
              id="key"
              type="password"
              placeholder="gw-…"
              value={key}
              onChange={(e) => setKey(e.target.value.trim())}
              autoComplete="off"
            />
            <button className="secondary" onClick={getDemoKey}>
              Get demo key
            </button>
          </div>
        </div>

        <div className="field">
          <label htmlFor="model">Model</label>
          <select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
            {models.length === 0 && <option value={model}>{model}</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id === "auto" ? "auto — smart routing" : `${m.id}  ·  ${m.owned_by}`}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <div className="label-row">
            <label htmlFor="prompt">Prompt</label>
            <div className="chips">
              {EXAMPLES.map((ex) => (
                <button key={ex.label} className="chip" onClick={() => setPrompt(ex.prompt)}>
                  {ex.label}
                </button>
              ))}
            </div>
          </div>
          <textarea
            id="prompt"
            rows={5}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
            }}
          />
        </div>

        <div className="options">
          <label className="toggle">
            <input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} />
            Stream
          </label>
          <label className="toggle" title="temperature 0: identical requests are served from the cache">
            <input
              type="checkbox"
              checked={deterministic}
              onChange={(e) => setDeterministic(e.target.checked)}
            />
            Cacheable (temperature 0)
          </label>
          <label
            className="toggle"
            title="Adds a visible system message telling the model its length budget"
          >
            <input type="checkbox" checked={concise} onChange={(e) => setConcise(e.target.checked)} />
            Concise (fit the limit)
          </label>
          <label className="toggle">
            Max tokens
            <input
              type="number"
              min={16}
              max={4096}
              step={16}
              value={maxTokens}
              onChange={(e) => setMaxTokens(Number(e.target.value))}
            />
          </label>
        </div>

        {providers.length > 0 && (
          <div className="field">
            <label>Simulate an outage (this request only)</label>
            <div className="chips">
              {providers.map((p) => (
                <button
                  key={p}
                  className={`chip ${down.includes(p) ? "chip-danger" : ""}`}
                  onClick={() => toggleDown(p)}
                  aria-pressed={down.includes(p)}
                >
                  {down.includes(p) ? "✕ " : ""}
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="actions">
          <button className="primary" onClick={() => void send()} disabled={!key || busy}>
            {busy ? "Running…" : "Send"}
          </button>
          <span className="hint">Ctrl + Enter</span>
        </div>
        {notice && <p className="notice">{notice}</p>}
      </section>

      <section className="panel result">
        <ResultView live={live} busy={busy} run={last} onContinue={continueRun} />
        {runs.length > 1 && <History runs={runs.slice(1)} />}
      </section>
    </div>
  );
}

function ResultView({
  live,
  busy,
  run,
  onContinue,
}: {
  live: { text: string; reasoning: string };
  busy: boolean;
  run: Run | undefined;
  onContinue: (run: Run) => void;
}) {
  const text = busy ? live.text : (run?.text ?? "");
  const reasoning = busy ? live.reasoning : (run?.reasoning ?? "");
  if (!busy && !run) {
    return (
      <div className="empty">
        <h3>Send a request</h3>
        <p>
          Try <code>auto</code> to watch the router pick a model, tick <em>Cacheable</em> and send
          twice to see a cache hit, or simulate an outage to watch the request fail over.
        </p>
      </div>
    );
  }
  return (
    <>
      {run && !busy && <Meta run={run} />}
      {run?.error && !busy && <div className="error-box">{run.error}</div>}
      {reasoning && (
        <details className="reasoning">
          <summary>Reasoning</summary>
          <p>{reasoning}</p>
        </details>
      )}
      <div className={`answer ${busy ? "streaming" : ""}`}>
        {text ? <Markdown text={text} /> : busy ? "…" : ""}
      </div>
      {run && !busy && run.finishReason === "length" && (
        <div className="warn-box">
          <span>
            The answer was cut off at the <strong>max tokens</strong> limit. Continue it, or raise
            Max tokens / turn on Concise and send again.
          </span>
          <button className="secondary" onClick={() => onContinue(run)}>
            Continue
          </button>
        </div>
      )}
    </>
  );
}

function Meta({ run }: { run: Run }) {
  const routed = run.route !== undefined;
  return (
    <div className="meta">
      {routed && (
        <Tile label="Route">
          <span className={`badge ${run.route === "cheap" ? "badge-green" : "badge-purple"}`}>
            {run.route}
          </span>
          <small>
            {run.routeReason}
            {run.routeProbability !== undefined && ` · P(cheap ok) ${run.routeProbability.toFixed(2)}`}
          </small>
        </Tile>
      )}
      <Tile label="Served by">
        <strong>{run.alias ?? "—"}</strong>
        <small>
          {run.provider} · {run.model}
        </small>
      </Tile>
      <Tile label="Failover">
        <strong>{run.fallbacks > 0 ? `${run.fallbacks} fallback${run.fallbacks > 1 ? "s" : ""}` : "none"}</strong>
        <small>{run.attempts} upstream attempt{run.attempts === 1 ? "" : "s"}</small>
      </Tile>
      <Tile label="Cache">
        <span className={`badge ${run.cache === "HIT" ? "badge-green" : "badge-gray"}`}>
          {run.cache ?? "—"}
        </span>
      </Tile>
      <Tile label="Cost">
        <strong>{usd(run.costUsd)}</strong>
        <small>
          {run.promptTokens ?? "?"} in · {run.completionTokens ?? "?"} out
        </small>
      </Tile>
      <Tile label="Latency">
        <strong>{ms(run.totalMs)}</strong>
        <small>
          {run.ttftMs !== undefined && `first token ${ms(run.ttftMs)} · `}
          gateway {ms(run.overheadMs)}
        </small>
      </Tile>
      <Tile label="Status">
        <span className={`badge ${run.ok ? "badge-green" : "badge-red"}`}>{run.status}</span>
        {run.finishReason && (
          <small>{run.finishReason === "length" ? "cut off (max tokens)" : run.finishReason}</small>
        )}
      </Tile>
    </div>
  );
}

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <div className="tile-body">{children}</div>
    </div>
  );
}

function History({ runs }: { runs: Run[] }) {
  return (
    <div className="history">
      <h4>Earlier runs</h4>
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Served by</th>
            <th>Cache</th>
            <th className="num">Latency</th>
            <th className="num">Cost</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} title={r.prompt}>
              <td>{r.requested}</td>
              <td>
                {r.alias ?? "—"}
                {r.fallbacks > 0 && <span className="badge badge-amber">failover</span>}
              </td>
              <td>{r.cache ?? "—"}</td>
              <td className="num">{ms(r.totalMs)}</td>
              <td className="num">{usd(r.costUsd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
