# 0008: Per-key rate limits use a token bucket in Redis

**Status:** accepted (M2b). Code: `gateway/limits/ratelimit.py`, `gateway/limits/lua/ratelimit_*.lua`.

## Decision
Each key has two buckets: requests/minute (RPM) and tokens/minute (TPM). A bucket holds up to
`limit` units and refills continuously at `limit / 60s`. One Lua script checks **both** and
deducts from both atomically, or from neither.

## Why a token bucket and not a sliding window

| | Token bucket | Sliding-window log | Fixed window |
|---|---|---|---|
| Redis state per key | 2 numbers | 1 entry *per request* (or per token!) | 1 counter |
| Bursts | allowed up to capacity, then smooth | none | **2× limit at the window boundary** |
| Works for TPM | yes: deduct N units | storing every token is impractical | yes, but has the boundary problem |
| Retry-After | exact: `(needed − available) / rate` | needs a scan | coarse |

Two things decided it:
- **TPM.** A log of individual requests can't be checked against a *token* budget without
  storing token counts and summing them on every call. A bucket just subtracts N.
- **The fixed-window boundary bug.** A client can send `limit` requests at 0:59 and `limit` more
  at 1:00. The bucket has no boundary.

The cost: a bucket allows a burst of up to `limit` at once, after an idle period. For an LLM
gateway that's what clients expect (OpenAI's own limits behave this way), and the upstream
providers we sit in front of handle short bursts fine.

## TPM is charged before we know the token count
We don't know how many tokens a request will use until it finishes. So:
1. **Up front:** charge `prompt_estimate + max_output` (`max_tokens`, or the default cap).
   This is the most the request can use.
2. **After:** `adjust_tokens(charged − actual)` refunds the difference. A cache hit refunds
   everything.

Charging the maximum means a burst of big-`max_tokens` requests is throttled even if they end up
short. That's conservative but never over-admits, and the refund restores capacity right away.
A single request larger than the whole bucket is admitted once the bucket is full and puts it
into debt. The alternative, rejecting it forever, would be a confusing permanent failure.

## Correctness across replicas
- The check-and-deduct is one Lua script, so two replicas can't both see 1 token left and both
  take it.
- *Tested:* 60 concurrent requests through **3 gateway instances** against one Redis with
  `rpm=10` → exactly 10 × 200 and 50 × 429. For TPM, `tpm=1000` with 108 tokens charged per
  request admitted exactly 9.

## Clocks go backwards: found in testing
Time comes from Redis (`TIME`), so all replicas agree. But on Docker Desktop, Redis's VM clock
was measured stepping **back by 750–950ms every few seconds** (time sync correcting drift). A
naive `tokens + (now − last) × rate` then *removes* tokens, over-throttling the key. NTP step
corrections and VM migrations can do the same in production. Fix: elapsed time is clamped at 0
and the stored timestamp never moves back, so a backward step only pauses the refill.
*Tested* by planting a future timestamp in the bucket.

## Failure mode
Redis unavailable → **503 (fail closed)**. A rate limit that silently stops working lets one key
starve everyone else and run up upstream bills. Keys with no limits configured never touch Redis
and keep working.

Headers: `X-RateLimit-Limit` / `-Remaining` (RPM), `X-RateLimit-Limit-Tokens` /
`-Remaining-Tokens` (TPM), and `Retry-After` on 429.
