# 0010: Exact-match response cache with request coalescing

**Status:** accepted (M2b). Code: `gateway/cache/response_cache.py`.

## What gets cached
Only requests whose output is (meant to be) deterministic:
- `temperature == 0`, or
- the client sends `X-Gateway-Cache: force`. They accept a fixed sample.

`X-Gateway-Cache: skip` opts out. Caching a `temperature: 0.7` request by default would silently
turn "give me a different answer" into "here's the same one again".

## Cache key
`sha256(canonical JSON of {api_key_id, request})`, with sorted keys and no whitespace. The
request includes **every field except** `stream`, `stream_options` and `user`.
- **Exclude-list, not include-list.** The spec's list (model, messages, temperature, top_p,
  max_tokens, tools, response_format) misses `stop`, `seed`, `n`, `tool_choice`,
  `max_completion_tokens` and any pass-through field like `logprobs`. Each of those changes the
  output, so leaving one out would return wrong cached answers. With an exclude-list, a new
  OpenAI parameter is included in the key automatically. The worst case is a missed hit, never a
  wrong one.
- **`stream` is excluded** so streamed and non-streamed requests share entries. A cached
  response is replayed as SSE chunks when the client asked for a stream, and a streamed answer
  is reassembled into a full response before it's cached.
- **Scoped per API key.** Sharing across keys would raise the hit rate, but `X-Cache: HIT` (and
  the 15ms vs 1s latency difference) would tell one tenant that *someone else* sent the exact
  same prompt. That's a side channel. Per-key is the safe default.

## Stampede protection (coalescing)
Without it, 100 identical requests arriving together all miss and make 100 upstream calls: the
cache is useless exactly when it matters most. With coalescing:
1. Check the cache: hit → return.
2. Miss → `SET lock NX PX (total_timeout + 5s)`. The winner (**leader**) calls upstream, stores
   the result, then deletes the lock.
3. Everyone else **polls** the cache with backoff (20ms doubling to 200ms). If the lock
   disappears without a value (the leader failed), the next poller becomes the leader. After
   `CACHE_WAIT_TIMEOUT_S` (30s) a waiter gives up and calls upstream itself, so a stuck leader
   never blocks anyone forever.

**Unlock is compare-and-delete** (Lua: delete only if the value is still my token). Otherwise a
leader whose lock already expired could delete the *next* leader's lock.

**Polling vs pub/sub:** pub/sub would wake waiters sooner, but needs a subscription connection
per waiter and has its own race (the result can be published before you subscribe). Polling is
a few cheap `GET`s, and its worst-case added latency (≤200ms) is small next to an LLM call.

*Tested:* 100 identical concurrent requests through 3 gateway instances → **exactly 1** upstream
call, 1 `MISS`, 99 `HIT`, all with the same content.

## What a hit costs
- $0 in the request log (`cache_hit = true`) and $0 against the spend cap (no reservation).
- The TPM estimate is refunded in full, but the request still counts toward RPM: it still
  costs the gateway work.
- Savings are tracked as `gateway_cache_saved_usd_total` and
  `gateway_cache_saved_seconds_total` (each hit saves what the original call cost and how long
  it took). `/admin/usage` reports hit rates.

Measured live: a miss took 1,013ms through OpenRouter; the hit took **15ms** and cost $0.

## Failure mode
Redis down → **fail open**: skip the cache and call the provider. A cache outage costs money and
latency, not correctness. Coalescing is also skipped (every request acts as its own leader).

Only successful, completed responses are cached. Errors, mid-stream failures and disconnected
streams are not.
