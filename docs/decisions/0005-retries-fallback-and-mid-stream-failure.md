# 0005: Retries, fallback chains, and what happens when a stream fails midway

**Status:** accepted (M2a). Code: `gateway/routing/dispatch.py`.

## The policy
For each candidate (the requested alias, then its `fallbacks` from `models.yaml` in order):

1. **Skip** it if its provider's circuit is open, or if the provider isn't configured, or if
   the API key isn't allowed to use that alias.
2. **Try** it up to 2 times. Retry only `RateLimited`, `ProviderUnavailable` and
   `ProviderTimeout`. Backoff is exponential with *full jitter*:
   `random(0, min(2s, 0.2s × 2^attempt))`.
3. Then **fail over** to the next candidate.

| Error | Retry same provider? | Fail over? | Counts toward breaker? |
|---|---|---|---|
| `RateLimited` (429) | yes. If `Retry-After` ≤ 2s we wait exactly that; if longer, we move on immediately | yes | yes |
| `ProviderUnavailable` (5xx, connection error, bad JSON) | yes | yes | yes |
| `ProviderTimeout` | yes | yes | yes |
| `AuthError` (our key for *that* provider is bad) | no, it won't fix itself | yes | yes |
| `BadRequest` (client's fault) | **no** | **no**, another model would reject it too | no |

All attempts share **one overall deadline** (`TOTAL_TIMEOUT_S`). Every attempt gets only the time
that's left, and we never sleep past the deadline. That way, retries can't multiply the client's
worst-case wait to 2 × 3 × 120s.

**Why full jitter:** when a provider recovers, every replica's retries would otherwise land at
the same moment and knock it over again (a thundering herd). Random delays spread them out.
This is the scheme from AWS's "Exponential Backoff and Jitter" article.

**Why a short retry cap (2 attempts, 2s maximum delay):** we have fallbacks. Waiting 30s on a
rate-limited provider is worse for the user than answering from a sibling model in 1s.

## Streaming: where the failover boundary is
The dispatcher **primes** the stream: it pulls chunks until the first one that contains content
(text or a tool call) before returning, and the handler only then commits to HTTP 200.

- **Failure before the first content chunk** (connection error, upstream 5xx, a role-only chunk
  followed by an error, first-token timeout): the client has seen nothing, so we fail over
  transparently. Chunks from the failed attempt are discarded. *Tested: a role chunk followed by
  an error from Groq, then the answer from OpenRouter; the client sees only OpenRouter's text.*
- **Failure after content was sent:** we **do not** fail over. The stream ends with
  `data: {"error": {...}}` and no `[DONE]`. The OpenAI SDK raises `APIError`. The failure is logged
  and counted toward the breaker.

### Why not splice another model's continuation onto a half-finished answer?
- **Correctness:** model B didn't write the first half. Its continuation can contradict it, repeat
  it or switch style. The result is text that *neither* model produced, and the client has no way
  to detect that.
- **Tool calls:** half a JSON argument string from one model plus the rest from another is
  invalid, or worse, valid but wrong.
- **Accounting:** you'd pay for two partial generations while reporting one response.
- **The alternative is cheap:** the client already handles errors and can retry the whole request.
  It's an honest failure instead of a silent corruption.

**What it costs:** a failure in the middle of a long answer means the user retries from scratch.
Failures before the first token are by far the most common case (rate limits, overload,
cold starts), and those are fully covered.

## Logging
`requests.alias` is what the client asked for. `provider`, `upstream_model` and `cost_usd` are
what actually served it (cost uses the *served* model's price). `fallback_count` is the index of
the serving candidate. The response headers expose the same information: `X-Gateway-Alias`,
`X-Gateway-Fallbacks` and `X-Gateway-Attempts`.
