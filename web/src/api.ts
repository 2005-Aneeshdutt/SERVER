// Typed client for the gateway. Same origin in production (nginx) and in dev (Vite proxy).

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, init);
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = (body as { error?: { message?: string; code?: string } } | null)?.error;
    throw new ApiError(res.status, err?.message ?? `${res.status} ${res.statusText}`, err?.code);
  }
  return body as T;
}

const bearer = (token: string): HeadersInit => ({ Authorization: `Bearer ${token}` });
const json = (token: string): HeadersInit => ({
  ...bearer(token),
  "Content-Type": "application/json",
});

// ---- health -------------------------------------------------------------------------------

export interface Readiness {
  ready: boolean;
  degraded: boolean;
  checks: Record<string, string>;
}

export const getReadiness = () => request<Readiness>("/readyz");

// ---- client API -----------------------------------------------------------------------------

export interface ModelInfo {
  id: string;
  owned_by: string;
}

export const listModels = (key: string) =>
  request<{ data: ModelInfo[] }>("/v1/models", { headers: bearer(key) }).then((r) => r.data);

export interface DemoKey {
  key: string;
  name: string;
  rpm_limit: number;
  monthly_spend_cap_usd: string;
  allowed_models: string[];
}

export const createDemoKey = () => request<DemoKey>("/demo/keys", { method: "POST" });

export interface Message {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatOptions {
  key: string;
  model: string;
  messages: Message[];
  stream: boolean;
  deterministic: boolean;
  maxTokens: number;
  simulateOutage: string[];
  /** OpenAI's reasoning_effort for reasoning models (gpt-oss); others ignore it. */
  reasoningEffort?: "low" | "medium" | "high";
}

/** Everything the gateway tells us about one call, from headers and the stream itself. */
export interface ChatResult {
  ok: boolean;
  status: number;
  text: string;
  reasoning: string;
  /** "length" means the answer was cut off by max_tokens. */
  finishReason?: string;
  error?: string;
  /** OpenAI-style error code from the gateway (e.g. provider_unavailable, spend_cap_exceeded). */
  errorCode?: string;
  totalMs: number;
  ttftMs?: number;
  provider?: string;
  model?: string;
  alias?: string;
  route?: string;
  routeReason?: string;
  routeProbability?: number;
  routeMs?: number;
  cache?: string;
  fallbacks: number;
  attempts: number;
  costUsd?: number;
  /** Time the gateway itself added (Server-Timing gateway;dur). */
  overheadMs?: number;
  /** Time waiting on the provider up to the first chunk (Server-Timing upstream;dur). */
  upstreamMs?: number;
  /** Client-measured time until response headers arrived. */
  headersMs?: number;
  promptTokens?: number;
  completionTokens?: number;
  requestId?: string;
  /** Set when a distilled (small fine-tuned) model took part, from X-Gateway-Distill. */
  distill?: DistillInfo;
}

/** Outcome of the distilled-model rollout for one request. */
export type DistillOutcome = "served" | "fallback" | "shadow" | "distilled";

export interface DistillInfo {
  task: string;
  /** Usually a DistillOutcome; kept as string so a new server value still displays. */
  outcome: DistillOutcome | (string & {});
}

/** Parse `X-Gateway-Distill: "<task>; <outcome>"`. */
export function parseDistill(value: string | null): DistillInfo | undefined {
  if (!value) return undefined;
  const cut = value.lastIndexOf(";");
  if (cut < 0) return { task: value.trim(), outcome: "distilled" };
  const task = value.slice(0, cut).trim();
  const outcome = value.slice(cut + 1).trim().toLowerCase();
  return task ? { task, outcome: outcome || "distilled" } : undefined;
}

interface Delta {
  content?: string | null;
  reasoning?: string | null;
  reasoning_content?: string | null;
}

interface StreamEvent {
  choices?: {
    delta?: Delta;
    message?: Delta & { content?: string | null };
    finish_reason?: string | null;
  }[];
  usage?: { prompt_tokens: number; completion_tokens: number };
  x_gateway?: { cost_usd: string };
  error?: { message?: string; code?: string };
}

function fromHeaders(res: Response): Partial<ChatResult> {
  const h = res.headers;
  const num = (name: string) => {
    const v = h.get(name);
    return v === null ? undefined : Number(v);
  };
  const timing = h.get("server-timing") ?? "";
  const overhead = /gateway;dur=([\d.]+)/.exec(timing)?.[1];
  const upstream = /upstream;dur=([\d.]+)/.exec(timing)?.[1];
  return {
    provider: h.get("x-gateway-provider") ?? undefined,
    model: h.get("x-gateway-model") ?? undefined,
    alias: h.get("x-gateway-alias") ?? undefined,
    route: h.get("x-gateway-route") ?? undefined,
    routeReason: h.get("x-gateway-route-reason") ?? undefined,
    routeProbability: num("x-gateway-route-probability"),
    routeMs: num("x-gateway-route-ms"),
    cache: h.get("x-cache") ?? undefined,
    fallbacks: num("x-gateway-fallbacks") ?? 0,
    attempts: num("x-gateway-attempts") ?? 0,
    costUsd: num("x-gateway-cost-usd"),
    overheadMs: overhead === undefined ? undefined : Number(overhead),
    upstreamMs: upstream === undefined ? undefined : Number(upstream),
    requestId: h.get("x-gateway-request-id") ?? undefined,
    distill: parseDistill(h.get("x-gateway-distill")),
  };
}

export async function chat(
  opts: ChatOptions,
  onText: (text: string, reasoning: string) => void,
): Promise<ChatResult> {
  const started = performance.now();
  const headers: Record<string, string> = {
    Authorization: `Bearer ${opts.key}`,
    "Content-Type": "application/json",
  };
  if (opts.simulateOutage.length) headers["X-Gateway-Simulate-Outage"] = opts.simulateOutage.join(",");
  const body = {
    model: opts.model,
    messages: opts.messages,
    max_tokens: opts.maxTokens,
    stream: opts.stream,
    ...(opts.stream ? { stream_options: { include_usage: true } } : {}),
    ...(opts.deterministic ? { temperature: 0 } : { temperature: 0.7 }),
    ...(opts.reasoningEffort ? { reasoning_effort: opts.reasoningEffort } : {}),
  };
  const res = await fetch("/v1/chat/completions", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const headersMs = performance.now() - started;
  const result: ChatResult = {
    ok: res.ok,
    status: res.status,
    text: "",
    reasoning: "",
    totalMs: 0,
    fallbacks: 0,
    attempts: 0,
    ...fromHeaders(res),
    headersMs,
  };
  if (!res.ok) {
    const err = (await res.json().catch(() => null)) as StreamEvent | null;
    result.error = err?.error?.message ?? `${res.status} ${res.statusText}`;
    result.errorCode = err?.error?.code;
  } else if (opts.stream && res.body) {
    await readStream(res.body, result, started, onText);
  } else {
    const data = (await res.json()) as StreamEvent;
    const message = data.choices?.[0]?.message;
    result.finishReason = data.choices?.[0]?.finish_reason ?? undefined;
    result.text = message?.content ?? "";
    result.reasoning = message?.reasoning ?? message?.reasoning_content ?? "";
    result.promptTokens = data.usage?.prompt_tokens;
    result.completionTokens = data.usage?.completion_tokens;
    onText(result.text, result.reasoning);
  }
  result.totalMs = performance.now() - started;
  return result;
}

async function readStream(
  stream: ReadableStream<Uint8Array>,
  result: ChatResult,
  started: number,
  onText: (text: string, reasoning: string) => void,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const event of events) {
      const data = event.replace(/^data: /, "").trim();
      if (!data || data === "[DONE]") continue;
      const parsed = JSON.parse(data) as StreamEvent;
      if (parsed.error) {
        result.ok = false;
        result.error = parsed.error.message ?? "stream failed";
        continue;
      }
      const finish = parsed.choices?.[0]?.finish_reason;
      if (finish) result.finishReason = finish;
      const delta = parsed.choices?.[0]?.delta;
      const piece = delta?.content ?? "";
      const thought = delta?.reasoning ?? delta?.reasoning_content ?? "";
      if ((piece || thought) && result.ttftMs === undefined) {
        result.ttftMs = performance.now() - started;
      }
      if (piece || thought) {
        result.text += piece;
        result.reasoning += thought;
        onText(result.text, result.reasoning);
      }
      if (parsed.usage) {
        result.promptTokens = parsed.usage.prompt_tokens;
        result.completionTokens = parsed.usage.completion_tokens;
      }
      if (parsed.x_gateway) result.costUsd = Number(parsed.x_gateway.cost_usd);
    }
  }
}

// ---- admin API ------------------------------------------------------------------------------

export interface Totals {
  requests: number;
  errors: number;
  cache_hits: number;
  cost_usd: string;
  tokens: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  fallbacks: number;
}

export interface Bucket {
  bucket: string;
  requests: number;
  errors: number;
  cache_hits: number;
  cost_usd: string;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
}

export interface Timeseries {
  bucket: "minute" | "hour";
  since: string;
  until: string;
  source: string;
  replica_lag_s: number | null;
  totals: Partial<Totals>;
  rows: Bucket[];
}

export const getTimeseries = (token: string, hours: number, bucket: "minute" | "hour") =>
  request<Timeseries>(`/admin/usage/timeseries?hours=${hours}&bucket=${bucket}`, {
    headers: bearer(token),
  });

export interface UsageRow {
  key_id: string | null;
  alias: string;
  requests: number;
  errors: number;
  cache_hits: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: string;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  fallbacks: number;
}

export const getUsage = (token: string, sinceIso: string) =>
  request<{ rows: UsageRow[] }>(`/admin/usage?since=${encodeURIComponent(sinceIso)}`, {
    headers: bearer(token),
  }).then((r) => r.rows);

export interface RecentRequest {
  id: string;
  created_at: string;
  key_name: string | null;
  alias: string;
  provider: string;
  upstream_model: string;
  status: string;
  error_type: string | null;
  latency_ms: number;
  time_to_first_token_ms: number | null;
  cost_usd: string;
  cache_hit: boolean;
  fallback_count: number;
  route: string | null;
  route_probability: number | null;
  prompt_tokens?: number;
  completion_tokens?: number;
}

export const getRecent = (token: string, limit = 25) =>
  request<{ rows: RecentRequest[]; source: string }>(`/admin/requests?limit=${limit}`, {
    headers: bearer(token),
  });

export interface ProviderStatus {
  name: string;
  configured: boolean;
  state: string;
  aliases: string[];
}

export const getProviders = (token: string) =>
  request<ProviderStatus[]>("/admin/providers", { headers: bearer(token) });

export const setOutage = (token: string, name: string, down: boolean) =>
  request<{ name: string; state: string }>(`/admin/providers/${name}/outage`, {
    method: "POST",
    headers: json(token),
    body: JSON.stringify({ down }),
  });

export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  rpm_limit: number | null;
  tpm_limit: number | null;
  monthly_spend_cap_usd: string | null;
  allowed_models: string[] | null;
  active: boolean;
  routing_quality: string;
  created_at: string | null;
}

export const listKeys = (token: string) =>
  request<ApiKey[]>("/admin/keys", { headers: bearer(token) });

export interface NewKey {
  name: string;
  rpm_limit?: number;
  monthly_spend_cap_usd?: string;
  routing_quality: string;
}

export const createKey = (token: string, key: NewKey) =>
  request<ApiKey & { key: string }>("/admin/keys", {
    method: "POST",
    headers: json(token),
    body: JSON.stringify(key),
  });

export const setKeyActive = (token: string, id: string, active: boolean) =>
  request<ApiKey>(`/admin/keys/${id}`, {
    method: "PATCH",
    headers: json(token),
    body: JSON.stringify({ active }),
  });

// ---- distilled-model rollout ----------------------------------------------------------------

export interface DistillTask {
  task: string;
  system_fingerprint: string;
  source_alias: string;
  distilled_alias: string;
  version: string;
  state: string;
  weight: number;
  shadow_n: number;
  shadow_valid: number;
  shadow_agree: number;
  window_n: number;
  window_failures: number;
}

export interface DistillAuditEvent {
  ts: string;
  task: string;
  action: string;
  state: string;
  weight: number;
  actor: string;
  reason: string;
}

/**
 * null when the rollout isn't available on this gateway, so the panel can hide: 503 when the
 * feature is disabled, 404 when the gateway build predates the endpoints.
 */
export async function getDistill(
  token: string,
): Promise<{ tasks: DistillTask[]; audit: DistillAuditEvent[] } | null> {
  try {
    const [tasks, audit] = await Promise.all([
      request<DistillTask[]>("/admin/distill/tasks", { headers: bearer(token) }),
      request<DistillAuditEvent[]>("/admin/distill/audit?limit=8", { headers: bearer(token) }),
    ]);
    return { tasks, audit };
  } catch (e) {
    if (e instanceof ApiError && (e.status === 503 || e.status === 404)) return null;
    throw e;
  }
}
