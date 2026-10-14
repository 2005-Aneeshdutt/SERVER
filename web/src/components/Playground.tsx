import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  chat,
  createDemoKey,
  listModels,
  type ChatResult,
  type DistillInfo,
  type Message,
  type ModelInfo,
} from "../api";
import { count, ms, usd } from "../format";
import { useCommand, useStored } from "../hooks";
import {
  IconActivity,
  IconArrowRight,
  IconCoins,
  IconColumns,
  IconDatabase,
  IconEye,
  IconEyeOff,
  IconFlask,
  IconHash,
  IconHistory,
  IconKey,
  IconPower,
  IconRoute,
  IconSend,
  IconShuffle,
  IconSparkles,
  IconTerminal,
  IconTrophy,
  IconZap,
} from "./icons";
import { Markdown } from "./Markdown";
import { useToast } from "./toast";
import { StackedBar, TraceRail, TraceStats, useElapsed, Waterfall, type LivePhase } from "./Trace";
import { Badge, Callout, Card, EmptyState, Kbd, Segmented, Skeleton, Switch, type Tone } from "./ui";

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
  streamed: boolean;
  compare?: "A" | "B";
}

interface LiveText {
  text: string;
  reasoning: string;
}

const EMPTY_LIVE: LiveText = { text: "", reasoning: "" };

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

type Notice = { tone: "info" | "ok" | "bad"; text: string };

const errorText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));

const phaseOf = (live: LiveText): LivePhase => (live.text || live.reasoning ? "streaming" : "waiting");

export function Playground() {
  const toast = useToast();
  const [key, setKey] = useStored("gw.key", "");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsState, setModelsState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [model, setModel] = useStored("gw.model", "auto");
  const [modelB, setModelB] = useStored("gw.modelB", "");
  const [mode, setMode] = useStored("gw.mode", "single");
  const compare = mode === "compare";
  const [prompt, setPrompt] = useState(EXAMPLES[0]!.prompt);
  const [stream, setStream] = useState(true);
  const [deterministic, setDeterministic] = useState(true);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [concise, setConcise] = useState(true);
  const [down, setDown] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [live, setLive] = useState<LiveText>(EMPTY_LIVE);
  const [liveB, setLiveB] = useState<LiveText>(EMPTY_LIVE);
  const [runs, setRuns] = useState<Run[]>([]);
  const [pair, setPair] = useState<[Run | null, Run | null] | null>(null);
  const [inflight, setInflight] = useState<{ a: string; b?: string; streamed: boolean } | null>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);

  useCommand((cmd) => {
    if (cmd.type === "model") setModel(cmd.model);
    else if (cmd.type === "compare") setMode(cmd.on ? "compare" : "single");
    else if (cmd.type === "focus-prompt") promptRef.current?.focus();
  });

  useEffect(() => {
    if (!key) {
      setModelsState("idle");
      return;
    }
    setModelsState("loading");
    listModels(key)
      .then((list) => {
        setModels(list);
        setModelsState("ready");
        if (!list.some((m) => m.id === model) && list[0]) setModel(list[0].id);
        setNotice((n) => (n?.tone === "bad" ? null : n));
      })
      .catch((e: unknown) => {
        setModelsState("error");
        setNotice({ tone: "bad", text: errorText(e) });
      });
  }, [key]);

  // Compare needs a second model: default to the first concrete alias that differs from A.
  useEffect(() => {
    if (!models.length) return;
    if (!models.some((m) => m.id === modelB) || modelB === model) {
      const other = models.find((m) => m.id !== model && m.id !== "auto") ?? models.find((m) => m.id !== model);
      if (other) setModelB(other.id);
    }
  }, [models, model]);

  const providers = useMemo(
    () => [...new Set(models.map((m) => m.owned_by).filter((p) => p !== "gateway"))].sort(),
    [models],
  );
  const last = runs.find((r) => !r.compare);

  async function getDemoKey() {
    setDemoBusy(true);
    try {
      const demo = await createDemoKey();
      setKey(demo.key);
      setNotice(null);
      toast("ok", "Demo key created", `${demo.rpm_limit} requests/min · ${usd(demo.monthly_spend_cap_usd)} spend cap`);
    } catch (e) {
      setNotice({ tone: "bad", text: errorText(e) });
    } finally {
      setDemoBusy(false);
    }
  }

  function buildMessages(): Message[] {
    return [...(concise ? [conciseInstruction(maxTokens)] : []), { role: "user", content: prompt }];
  }

  async function runOne(target: string, messages: Message[], onLive: (l: LiveText) => void): Promise<Run> {
    const result = await chat(
      {
        key,
        model: target,
        messages,
        stream,
        deterministic,
        maxTokens,
        simulateOutage: down,
        reasoningEffort: concise ? "low" : undefined,
      },
      (text, reasoning) => onLive({ text, reasoning }),
    );
    return { ...result, id: Date.now() + Math.random(), requested: target, prompt, messages, streamed: stream };
  }

  async function send() {
    if (!key || !prompt.trim() || busy) return;
    if (compare) return sendCompare();
    const messages = buildMessages();
    setBusy(true);
    setLive(EMPTY_LIVE);
    setInflight({ a: model, streamed: stream });
    try {
      const run = await runOne(model, messages, setLive);
      setRuns((prev) => [run, ...prev].slice(0, 10));
    } catch (e) {
      setNotice({ tone: "bad", text: String(e) });
    } finally {
      setBusy(false);
      setInflight(null);
    }
  }

  async function sendCompare() {
    const messages = buildMessages();
    setBusy(true);
    setLive(EMPTY_LIVE);
    setLiveB(EMPTY_LIVE);
    setPair([null, null]);
    setInflight({ a: model, b: modelB, streamed: stream });
    const settle = (slot: 0 | 1) => (run: Run) => {
      const tagged: Run = { ...run, compare: slot === 0 ? "A" : "B" };
      setPair((p) => {
        const nextPair: [Run | null, Run | null] = p ? [...p] : [null, null];
        nextPair[slot] = tagged;
        return nextPair;
      });
      setRuns((prev) => [tagged, ...prev].slice(0, 10));
      return tagged;
    };
    try {
      await Promise.all([
        runOne(model, messages, setLive).then(settle(0)),
        runOne(modelB, messages, setLiveB).then(settle(1)),
      ]);
    } catch (e) {
      setNotice({ tone: "bad", text: String(e) });
    } finally {
      setBusy(false);
      setInflight(null);
    }
  }

  /** Resume an answer that hit max_tokens: send it back as context and append the rest. */
  async function continueRun(run: Run) {
    if (!key || busy) return;
    const messages: Message[] = [...run.messages, { role: "assistant", content: run.text }, CONTINUE];
    setBusy(true);
    setLive({ text: run.text, reasoning: run.reasoning });
    setInflight({ a: run.requested, streamed: stream });
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
      setNotice({ tone: "bad", text: String(e) });
    } finally {
      setBusy(false);
      setInflight(null);
    }
  }

  const toggleDown = (p: string) =>
    setDown((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]));

  const modelOptions = (exclude?: string) =>
    models.length === 0 ? (
      <option value={exclude === undefined ? model : modelB}>{exclude === undefined ? model : modelB || "—"}</option>
    ) : (
      models.map((m) => (
        <option key={m.id} value={m.id} disabled={m.id === exclude}>
          {m.id === "auto" ? "auto — smart routing" : `${m.id}  ·  ${m.owned_by}`}
        </option>
      ))
    );

  return (
    <div className="playground">
      <div className="page-head playground__head">
        <div>
          <h1 className="page-title">Playground</h1>
          <p className="page-sub">
            Send OpenAI-format requests through the gateway and watch every stage: limits, cache, routing,
            failover, cost.
          </p>
        </div>
        <div className="page-head__actions">
          <Segmented
            label="Playground mode"
            value={compare ? "compare" : "single"}
            onChange={setMode}
            options={[
              {
                value: "single",
                label: (
                  <>
                    <IconTerminal size={13} /> Single
                  </>
                ),
              },
              {
                value: "compare",
                label: (
                  <>
                    <IconColumns size={13} /> Compare
                  </>
                ),
                title: "Send the same prompt to two models in parallel",
              },
            ]}
          />
        </div>
      </div>

      <Card className="compose" title="Request" icon={<IconSparkles />}>
        <form
          className="compose__form"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <KeyField
            value={key}
            onChange={setKey}
            onDemo={() => void getDemoKey()}
            demoBusy={demoBusy}
            modelsState={modelsState}
            modelCount={models.length}
          />

          {compare ? (
            <div className="field">
              <span className="field__label">Models</span>
              <div className="compare-pick">
                <span className="compare-pick__tag">A</span>
                <div className="select">
                  <select aria-label="Model A" value={model} onChange={(e) => setModel(e.target.value)}>
                    {modelOptions()}
                  </select>
                </div>
                <span className="compare-pick__tag compare-pick__tag--b">B</span>
                <div className="select">
                  <select aria-label="Model B" value={modelB} onChange={(e) => setModelB(e.target.value)}>
                    {modelOptions(model)}
                  </select>
                </div>
              </div>
              <p className="field__hint">Both requests leave at the same moment and stream side by side.</p>
            </div>
          ) : (
            <div className="field">
              <label className="field__label" htmlFor="model">
                Model
              </label>
              <div className="select">
                <select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
                  {modelOptions()}
                </select>
              </div>
              {model === "auto" && (
                <p className="field__hint">
                  A classifier picks a cheap or strong model per prompt. No LLM call on the hot path.
                </p>
              )}
            </div>
          )}

          <div className="field">
            <div className="field__row">
              <label className="field__label" htmlFor="prompt">
                Prompt
              </label>
              <div className="chips" role="group" aria-label="Example prompts">
                {EXAMPLES.map((ex) => (
                  <button
                    type="button"
                    key={ex.label}
                    className={`chip ${prompt === ex.prompt ? "is-active" : ""}`}
                    onClick={() => setPrompt(ex.prompt)}
                  >
                    {ex.label}
                  </button>
                ))}
              </div>
            </div>
            <textarea
              id="prompt"
              ref={promptRef}
              rows={5}
              value={prompt}
              placeholder="Ask anything…"
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
          </div>

          <fieldset className="fieldset">
            <legend className="field__label">Options</legend>
            <div className="switch-list">
              <Switch checked={stream} onChange={setStream} label="Stream" hint="Server-sent events" />
              <Switch
                checked={deterministic}
                onChange={setDeterministic}
                label="Cacheable"
                hint="temperature 0, identical requests hit the cache"
              />
              <Switch
                checked={concise}
                onChange={setConcise}
                label="Concise"
                hint="adds a system message with the length budget"
              />
              <div className="switch">
                <span className="switch__text">
                  <label className="switch__label" htmlFor="max-tokens">
                    Max tokens
                  </label>
                  <span className="switch__hint">answers past this are cut off</span>
                </span>
                <input
                  id="max-tokens"
                  className="input input--num"
                  type="number"
                  min={16}
                  max={4096}
                  step={16}
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(Number(e.target.value))}
                />
              </div>
            </div>
          </fieldset>

          {providers.length > 0 && (
            <fieldset className="fieldset">
              <legend className="field__label">Simulate an outage</legend>
              <p className="field__hint">
                Marks providers as down for this request only, so you can watch it fail over.
              </p>
              <div className="chips">
                {providers.map((p) => {
                  const off = down.includes(p);
                  return (
                    <button
                      type="button"
                      key={p}
                      className={`chip chip--toggle ${off ? "is-down" : ""}`}
                      onClick={() => toggleDown(p)}
                      aria-pressed={off}
                      aria-label={`${p}: ${off ? "simulated down" : "up"}`}
                    >
                      <IconPower size={12} />
                      {p}
                    </button>
                  );
                })}
              </div>
            </fieldset>
          )}

          <div className="compose__actions">
            <button
              className="btn btn--primary btn--lg btn--block btn--glow"
              type="submit"
              disabled={!key || busy || (compare && !modelB)}
            >
              {busy ? <span className="spinner" aria-hidden="true" /> : compare ? <IconColumns /> : <IconSend />}
              {busy ? "Running…" : compare ? "Run comparison" : "Send request"}
            </button>
            <span className="compose__hint">
              <Kbd>Ctrl</Kbd> <Kbd>Enter</Kbd>
            </span>
          </div>
          {!key && <p className="field__hint compose__nokey">Get a demo key or paste one to send a request.</p>}
          {notice && (
            <Callout tone={notice.tone} onDismiss={() => setNotice(null)}>
              {notice.text}
            </Callout>
          )}
        </form>
      </Card>

      <div className="results">
        {compare ? (
          <CompareView
            pair={pair}
            busy={busy}
            inflight={inflight}
            liveA={live}
            liveB={liveB}
            modelA={model}
            modelB={modelB}
          />
        ) : (
          <ResultCard
            live={live}
            busy={busy}
            run={busy ? undefined : last}
            requested={inflight?.a ?? model}
            streamed={inflight?.streamed ?? last?.streamed ?? stream}
            onContinue={continueRun}
            continuing={busy && !!last && live.text.startsWith(last.text) && last.text.length > 0}
          />
        )}
        {runs.length > (compare ? 0 : 1) && <History runs={compare ? runs : runs.filter((r) => r !== last)} />}
      </div>
    </div>
  );
}

function KeyField({
  value,
  onChange,
  onDemo,
  demoBusy,
  modelsState,
  modelCount,
}: {
  value: string;
  onChange: (v: string) => void;
  onDemo: () => void;
  demoBusy: boolean;
  modelsState: "idle" | "loading" | "ready" | "error";
  modelCount: number;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="field">
      <div className="field__row">
        <label className="field__label" htmlFor="key">
          API key
        </label>
        <KeyStatus state={modelsState} count={modelCount} />
      </div>
      <div className="key-row">
        <div className="input-group">
          <IconKey className="icon input-group__icon" />
          <input
            id="key"
            className="input input--mono"
            type={show ? "text" : "password"}
            placeholder="gw-…"
            value={value}
            onChange={(e) => onChange(e.target.value.trim())}
            autoComplete="off"
            spellCheck={false}
          />
          <button
            type="button"
            className="icon-btn input-group__btn"
            onClick={() => setShow(!show)}
            aria-label={show ? "Hide key" : "Show key"}
            aria-pressed={show}
          >
            {show ? <IconEyeOff /> : <IconEye />}
          </button>
        </div>
        <button type="button" className="btn btn--secondary" onClick={onDemo} disabled={demoBusy}>
          {demoBusy ? <span className="spinner" aria-hidden="true" /> : <IconZap />}
          Get demo key
        </button>
      </div>
    </div>
  );
}

function KeyStatus({ state, count: n }: { state: "idle" | "loading" | "ready" | "error"; count: number }) {
  if (state === "loading") return <span className="key-status">Checking…</span>;
  if (state === "ready")
    return (
      <span className="key-status key-status--ok">
        <span className="status-dot status-dot--ok" aria-hidden="true" /> Connected · {n} models
      </span>
    );
  if (state === "error")
    return (
      <span className="key-status key-status--bad">
        <span className="status-dot status-dot--bad" aria-hidden="true" /> Key rejected
      </span>
    );
  return null;
}

// ---- single run -----------------------------------------------------------------------------

function ResultCard({
  live,
  busy,
  run,
  requested,
  streamed,
  onContinue,
  continuing,
}: {
  live: LiveText;
  busy: boolean;
  run: Run | undefined;
  requested: string;
  streamed: boolean;
  onContinue: (run: Run) => void;
  continuing: boolean;
}) {
  const text = busy ? live.text : (run?.text ?? "");
  const reasoning = busy ? live.reasoning : (run?.reasoning ?? "");
  const phase = phaseOf(live);
  const elapsed = useElapsed(busy);
  const status = busy ? (
    <Badge tone="accent" dot>
      {phase === "streaming" ? "Streaming" : "In flight"} · {ms(elapsed)}
    </Badge>
  ) : run ? (
    <Badge tone={run.ok ? "ok" : "bad"} dot>
      {run.status} {run.ok ? "OK" : "Error"}
    </Badge>
  ) : null;

  return (
    <Card
      className="result"
      title="Response"
      icon={<IconArrowRight />}
      actions={
        <>
          {run?.requestId && !busy && (
            <code className="request-id" title="X-Gateway-Request-Id">
              {run.requestId.slice(0, 8)}
            </code>
          )}
          {status}
        </>
      }
    >
      {!busy && !run ? (
        <EmptyState icon={<IconSparkles size={20} />} title="Send a request to see it here">
          <ul className="tips">
            <li>
              Pick <code>auto</code> to watch the router choose a model and explain why.
            </li>
            <li>
              Keep <strong>Cacheable</strong> on and send twice to get an instant, free cache hit.
            </li>
            <li>Simulate an outage to watch the request fail over to the next provider.</li>
            <li>
              Switch to <strong>Compare</strong> to race two models on the same prompt.
            </li>
          </ul>
        </EmptyState>
      ) : (
        <div className="result__body" aria-busy={busy}>
          {busy || !run ? <ServedSkeleton requested={requested} /> : <Served run={run} />}
          {run?.distill && !busy && <DistillIndicator info={run.distill} />}

          <section className={`trace ${busy ? "is-live" : ""}`} aria-label="Request trace">
            <header className="trace__head">
              <span className="eyebrow">
                <IconActivity size={12} /> Request trace
              </span>
              {busy && <span className="trace__clock">{ms(elapsed)}</span>}
            </header>
            <TraceRail
              result={busy ? undefined : run}
              live={busy ? phase : undefined}
              streamed={streamed}
              routed={busy ? requested === "auto" : run?.route !== undefined}
            />
            {busy && (
              <div className="trace__progress" aria-hidden="true">
                <span />
              </div>
            )}
            {!busy && run && run.ok && (
              <>
                <TraceStats result={run} />
                <Waterfall result={run} streamed={run.streamed} />
              </>
            )}
          </section>

          {!busy && run && <MetaRow run={run} />}
          {run?.error && !busy && (
            <Callout tone="bad" title="Request failed">
              {run.error}
            </Callout>
          )}
          {reasoning && (
            <details className="reasoning">
              <summary>
                <IconSparkles size={14} /> Reasoning
                <span className="reasoning__count">{count(reasoning.length)} chars</span>
              </summary>
              <p>{reasoning}</p>
            </details>
          )}
          <div className={`answer ${busy ? "is-streaming" : ""}`} aria-live={busy ? "off" : "polite"}>
            {text ? (
              <Markdown text={text} />
            ) : busy ? (
              <div className="answer__placeholder">
                <Skeleton width="92%" />
                <Skeleton width="78%" />
                <Skeleton width="64%" />
              </div>
            ) : run?.ok ? (
              <p className="muted">The model returned an empty answer.</p>
            ) : null}
          </div>
          {run && !busy && run.finishReason === "length" && !continuing && (
            <Callout
              tone="warn"
              title="Cut off at the max tokens limit"
              action={
                <button type="button" className="btn btn--secondary btn--sm" onClick={() => onContinue(run)}>
                  Continue
                </button>
              }
            >
              Continue the answer, or raise Max tokens / turn on Concise and send again.
            </Callout>
          )}
        </div>
      )}
    </Card>
  );
}

function ServedSkeleton({ requested }: { requested: string }) {
  return (
    <div className="served" aria-hidden="true">
      <div className="served__main">
        <span className="eyebrow">Requested</span>
        <span className="served__alias">{requested}</span>
        <Skeleton width={200} height={10} />
      </div>
    </div>
  );
}

const ROUTE_TONE: Record<string, Tone> = { cheap: "ok", strong: "purple" };

function Served({ run }: { run: Run }) {
  const routed = run.route !== undefined;
  return (
    <div className="served">
      <div className="served__main">
        <span className="eyebrow">Answered by</span>
        <span className="served__alias" title="X-Gateway-Alias">
          {run.alias ?? "—"}
        </span>
        <span className="served__sub">
          {run.provider ?? "unknown provider"}
          {run.model && (
            <>
              <span aria-hidden="true"> · </span>
              <code>{run.model}</code>
            </>
          )}
          {run.requested !== run.alias && (
            <>
              <span aria-hidden="true"> · </span>requested <code>{run.requested}</code>
            </>
          )}
        </span>
      </div>
      {routed && (
        <div className="route" title="Smart routing decision (X-Gateway-Route)">
          <span className="eyebrow">
            <IconRoute size={12} /> Route
          </span>
          <div className="route__line">
            <Badge tone={ROUTE_TONE[run.route ?? ""] ?? "accent"}>{run.route}</Badge>
            {run.routeProbability !== undefined && (
              <span className="route__prob">
                P(cheap ok) <strong>{run.routeProbability.toFixed(2)}</strong>
              </span>
            )}
          </div>
          {run.routeProbability !== undefined && (
            <span className="prob-bar" aria-hidden="true">
              <span style={{ width: `${Math.max(0, Math.min(1, run.routeProbability)) * 100}%` }} />
            </span>
          )}
          {run.routeReason && <span className="route__reason">{run.routeReason}</span>}
        </div>
      )}
    </div>
  );
}

function MetaRow({ run }: { run: Run }) {
  const cacheHit = run.cache === "HIT";
  return (
    <dl className="metrics metrics--4">
      <Metric
        icon={<IconCoins />}
        label="Cost"
        value={usd(run.costUsd)}
        sub={cacheHit ? "free (cache hit)" : undefined}
      />
      <Metric
        icon={<IconHash />}
        label="Tokens"
        value={`${run.promptTokens ?? "?"} → ${run.completionTokens ?? "?"}`}
        sub="prompt → completion"
      />
      <Metric
        icon={<IconDatabase />}
        label="Cache"
        value={<Badge tone={cacheHit ? "ok" : "neutral"}>{run.cache ?? "—"}</Badge>}
      />
      <Metric
        icon={<IconShuffle />}
        label="Failover"
        value={
          run.fallbacks > 0 ? (
            <Badge tone="warn">
              {run.fallbacks} fallback{run.fallbacks > 1 ? "s" : ""}
            </Badge>
          ) : (
            "None"
          )
        }
        sub={`${run.attempts} upstream attempt${run.attempts === 1 ? "" : "s"}${
          run.finishReason ? ` · ${run.finishReason === "length" ? "cut off" : run.finishReason}` : ""
        }`}
      />
    </dl>
  );
}

function Metric({
  icon,
  label,
  value,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  sub?: string;
}) {
  return (
    <div className="metric">
      <dt className="metric__label">
        {icon}
        {label}
      </dt>
      <dd className="metric__value">{value}</dd>
      {sub && <dd className="metric__sub">{sub}</dd>}
    </div>
  );
}

const DISTILL_COPY: Record<string, { tone: Tone; title: string; detail: string }> = {
  served: {
    tone: "ok",
    title: "Served by distilled model",
    detail: "A small fine-tuned model answered and passed validation.",
  },
  distilled: {
    tone: "ok",
    title: "Routed to distilled model",
    detail: "This request was sent to the small fine-tuned model for its task.",
  },
  fallback: {
    tone: "warn",
    title: "Fallback to strong model",
    detail: "The distilled model's answer failed validation, so the source model answered instead.",
  },
  shadow: {
    tone: "purple",
    title: "Shadow comparison",
    detail: "The source model answered; the distilled model ran in the background for evaluation.",
  },
};

function distillCopy(info: DistillInfo) {
  return (
    DISTILL_COPY[info.outcome] ?? {
      tone: "accent" as Tone,
      title: `Distilled model · ${info.outcome}`,
      detail: "A distilled model took part in this request.",
    }
  );
}

function DistillIndicator({ info }: { info: DistillInfo }) {
  const copy = distillCopy(info);
  return (
    <div className={`distill-flag distill-flag--${copy.tone}`} role="note" title="X-Gateway-Distill">
      <span className="distill-flag__icon">
        <IconFlask />
      </span>
      <div className="distill-flag__text">
        <span className="distill-flag__title">
          {copy.title}
          <span className="distill-flag__task">
            <code>{info.task}</code>
          </span>
        </span>
        <span className="distill-flag__detail">{copy.detail}</span>
      </div>
      <Badge tone={copy.tone}>{info.outcome}</Badge>
    </div>
  );
}

// ---- compare --------------------------------------------------------------------------------

function CompareView({
  pair,
  busy,
  inflight,
  liveA,
  liveB,
  modelA,
  modelB,
}: {
  pair: [Run | null, Run | null] | null;
  busy: boolean;
  inflight: { a: string; b?: string; streamed: boolean } | null;
  liveA: LiveText;
  liveB: LiveText;
  modelA: string;
  modelB: string;
}) {
  if (!pair) {
    return (
      <Card title="Compare" icon={<IconColumns />}>
        <EmptyState icon={<IconColumns size={20} />} title="Race two models on one prompt">
          Pick model A and model B, then run. Both requests leave at the same moment; you get latency, cost,
          tokens and a trace for each, side by side.
        </EmptyState>
      </Card>
    );
  }
  const [a, b] = pair;
  const bothOk = a?.ok && b?.ok;
  const faster = bothOk && a && b ? (a.totalMs <= b.totalMs ? "A" : "B") : null;
  const cheaper =
    bothOk && a && b && (a.costUsd ?? 0) !== (b.costUsd ?? 0)
      ? (a.costUsd ?? 0) < (b.costUsd ?? 0)
        ? "A"
        : "B"
      : null;
  return (
    <div className="compare">
      {a && b && <Verdict a={a} b={b} faster={faster} cheaper={cheaper} />}
      <div className="compare__grid">
        <ComparePane
          slot="A"
          run={a}
          live={liveA}
          busy={busy && !a}
          requested={inflight?.a ?? a?.requested ?? modelA}
          streamed={inflight?.streamed ?? a?.streamed ?? true}
          wins={[faster === "A" && "Fastest", cheaper === "A" && "Cheapest"].filter(Boolean) as string[]}
        />
        <ComparePane
          slot="B"
          run={b}
          live={liveB}
          busy={busy && !b}
          requested={inflight?.b ?? b?.requested ?? modelB}
          streamed={inflight?.streamed ?? b?.streamed ?? true}
          wins={[faster === "B" && "Fastest", cheaper === "B" && "Cheapest"].filter(Boolean) as string[]}
        />
      </div>
    </div>
  );
}

function Verdict({ a, b, faster, cheaper }: { a: Run; b: Run; faster: string | null; cheaper: string | null }) {
  if (!a.ok || !b.ok) {
    return (
      <Callout tone="warn" title="One side failed">
        Compare the traces below to see where it stopped.
      </Callout>
    );
  }
  const dt = Math.abs(a.totalMs - b.totalMs);
  const ca = a.costUsd ?? 0;
  const cb = b.costUsd ?? 0;
  const ratio = Math.min(ca, cb) > 0 ? Math.max(ca, cb) / Math.min(ca, cb) : null;
  const name = (slot: string | null) => (slot === "A" ? (a.alias ?? a.requested) : (b.alias ?? b.requested));
  return (
    <div className="verdict" role="status">
      <span className="verdict__icon">
        <IconTrophy size={16} />
      </span>
      <p>
        <strong>{name(faster)}</strong> finished {ms(dt)} faster
        {cheaper && (
          <>
            {" · "}
            <strong>{name(cheaper)}</strong> was{" "}
            {ratio ? `${ratio >= 10 ? Math.round(ratio) : ratio.toFixed(1)}× cheaper` : "cheaper"}
          </>
        )}
        {!cheaper && " · same cost"}
      </p>
    </div>
  );
}

function ComparePane({
  slot,
  run,
  live,
  busy,
  requested,
  streamed,
  wins,
}: {
  slot: "A" | "B";
  run: Run | null;
  live: LiveText;
  busy: boolean;
  requested: string;
  streamed: boolean;
  wins: string[];
}) {
  const elapsed = useElapsed(busy);
  const text = run ? run.text : live.text;
  const phase = phaseOf(live);
  return (
    <section className={`pane ${wins.length ? "is-winner" : ""}`} aria-label={`Model ${slot}`}>
      <header className="pane__head">
        <span className={`compare-pick__tag ${slot === "B" ? "compare-pick__tag--b" : ""}`}>{slot}</span>
        <div className="pane__titles">
          <span className="pane__alias">{run?.alias ?? requested}</span>
          <span className="pane__sub">
            {run ? (
              <>
                {run.provider} · <code>{run.model}</code>
              </>
            ) : (
              `requested ${requested}`
            )}
          </span>
        </div>
        {busy ? (
          <Badge tone="accent" dot>
            {ms(elapsed)}
          </Badge>
        ) : run ? (
          <Badge tone={run.ok ? "ok" : "bad"} dot>
            {run.status}
          </Badge>
        ) : null}
      </header>
      {wins.length > 0 && (
        <div className="pane__wins">
          {wins.map((w) => (
            <span key={w} className="win">
              <IconTrophy size={12} /> {w}
            </span>
          ))}
        </div>
      )}
      <dl className="pane__stats">
        <div>
          <dt>Total</dt>
          <dd>{run ? ms(run.totalMs) : busy ? <Skeleton width={48} height={14} /> : "—"}</dd>
        </div>
        <div>
          <dt>First token</dt>
          <dd>{run ? ms(run.ttftMs) : busy ? <Skeleton width={48} height={14} /> : "—"}</dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>{run ? usd(run.costUsd) : busy ? <Skeleton width={48} height={14} /> : "—"}</dd>
        </div>
        <div>
          <dt>Tokens out</dt>
          <dd>{run ? (run.completionTokens ?? "?") : busy ? <Skeleton width={36} height={14} /> : "—"}</dd>
        </div>
      </dl>
      {busy ? (
        <TraceRail live={phase} streamed={streamed} routed={requested === "auto"} />
      ) : run ? (
        run.ok ? (
          <div className="pane__bar">
            <StackedBar result={run} streamed={run.streamed} />
            <span className="pane__bar-legend">
              gateway {ms(run.overheadMs)} · provider {run.cache === "HIT" ? "cache hit" : ms(run.upstreamMs)}
              {run.cache === "HIT" && " · $0"}
            </span>
          </div>
        ) : (
          <TraceRail result={run} streamed={run.streamed} routed={run.route !== undefined} />
        )
      ) : null}
      {run?.error && <Callout tone="bad">{run.error}</Callout>}
      <div className={`answer answer--compact ${busy ? "is-streaming" : ""}`}>
        {text ? (
          <Markdown text={text} />
        ) : busy ? (
          <div className="answer__placeholder">
            <Skeleton width="90%" />
            <Skeleton width="72%" />
            <Skeleton width="58%" />
          </div>
        ) : null}
      </div>
    </section>
  );
}

// ---- history --------------------------------------------------------------------------------

function History({ runs }: { runs: Run[] }) {
  return (
    <Card title="Earlier runs" icon={<IconHistory />} flush description="This session only, newest first.">
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Prompt</th>
              <th scope="col">Requested</th>
              <th scope="col">Answered by</th>
              <th scope="col">Cache</th>
              <th scope="col">Timing</th>
              <th scope="col" className="num">
                Latency
              </th>
              <th scope="col" className="num">
                Cost
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="cell-truncate" title={r.prompt}>
                  {r.compare && <span className="compare-pick__tag compare-pick__tag--xs">{r.compare}</span>}
                  {r.prompt}
                </td>
                <td>
                  <code className="code-pill">{r.requested}</code>
                </td>
                <td>
                  <div className="cell-stack">
                    <span className="cell-strong">{r.alias ?? "—"}</span>
                    <span className="cell-badges">
                      {!r.ok && <Badge tone="bad">{r.status}</Badge>}
                      {r.fallbacks > 0 && <Badge tone="warn">failover</Badge>}
                      {r.distill && (
                        <Badge tone={distillCopy(r.distill).tone} title={distillCopy(r.distill).title}>
                          distill · {r.distill.outcome}
                        </Badge>
                      )}
                    </span>
                  </div>
                </td>
                <td>
                  <Badge tone={r.cache === "HIT" ? "ok" : "neutral"}>{r.cache ?? "—"}</Badge>
                </td>
                <td className="cell-bar">{r.ok ? <StackedBar result={r} streamed={r.streamed} /> : "—"}</td>
                <td className="num">{ms(r.totalMs)}</td>
                <td className="num">{usd(r.costUsd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
