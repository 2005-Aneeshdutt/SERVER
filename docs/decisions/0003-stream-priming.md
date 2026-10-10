# 0003: Prime streams before committing to a 200 response

**Status:** accepted (M1). M2 builds failover on top of this.

## Problem
For `stream: true`, once we send the HTTP status line and headers, the status is fixed at 200.
If we start the response immediately and the upstream then returns 429 or 503, or never sends a
token, the client gets a "successful" response containing an error event. Clients that only check
the status code (and retry logic keyed on status codes) handle that badly.

## Decision
Before creating the `StreamingResponse`, the handler pulls the **first chunk** from the upstream
stream, with the connect and first-token timeouts applied. Then:
- If that fails (connection error, upstream 4xx/5xx, first-token timeout), we return a normal JSON
  error with the right status code (429/502/504) and `Retry-After` where relevant.
- If it succeeds, we commit to 200 and replay the primed chunk followed by the rest.
- If something fails *after* that, we can only report it in-band: a final
  `data: {"error": {...}}` event and no `[DONE]`. OpenAI SDKs raise `APIError` on that event.
  We never splice in output from another model mid-answer. The M2 ADR on mid-stream failover
  covers this in detail.

## Tradeoff
Headers go out roughly when the first chunk arrives instead of immediately. That doesn't matter,
because a client can't render anything before the first chunk anyway. What we gain is that every
failure before the first token is a real HTTP error. In M2 that is exactly the window where we can
retry or fail over to another provider without the client ever knowing.

## Related: timeouts
`providers/timeouts.py` applies three separate budgets around any provider:
- **connect** (httpx): fail fast on an unreachable host.
- **first-token**: catches a provider that accepts the request and then sits in a queue. A
  role-only chunk does *not* count; we wait for real content.
- **total**: caps a generation that keeps going.
