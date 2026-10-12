# 0009: Monthly spend caps use reserve-then-settle in Redis

**Status:** accepted (M2b). Code: `gateway/limits/spend.py`, `gateway/limits/lua/spend_*.lua`.

## The problem with "check, then charge"
The naive version reads `spent`, compares it to the cap, calls upstream, then adds the cost.
With a $1 cap, $0.99 spent and 50 requests arriving at once, all 50 read $0.99, all 50 pass,
and the key ends up far over its cap. The cost is only known *after* the call, so a simple
atomic counter doesn't help.

## Decision: reserve, then settle
1. **Reserve (before calling upstream).** Atomically, in Lua:
   if `spent + reserved + worst_case > cap`, reject with **402**; otherwise add `worst_case` to
   `reserved`. Concurrent requests see each other's reservations, so together they can never
   exceed the cap.
2. **Settle (after: success, error or client disconnect).** Release the reservation and add the
   **actual** cost to `spent`. A failed request settles at $0.
3. **Expiry.** Each reservation has a TTL (5 min). If a replica crashes mid-request, the next
   reserve call on that key purges the stale reservation, so budget can't leak forever.

*Tested:* 40 parallel requests through 3 replicas with a $0.001 cap and a worst case of
$0.0001525 each → exactly 6 admitted (⌊0.001 / 0.0001525⌋), 34 × 402. Also tested directly with
5 Redis connections and 100 concurrent reservations of $0.07 against $1: exactly 14 succeed.

## Making "worst case" actually the worst case
- **Output:** if the client sent no `max_tokens`, the gateway **sets** it to the default cap (1024).
  Otherwise the model could generate more than we reserved for. The cost is a hard output limit
  on capped keys, which is reasonable for a key with a budget.
- **Prompt:** our tiktoken count × **1.25**, because other providers' tokenizers count
  differently.
- **Model:** a fallback may serve the request, so we reserve at the **most expensive candidate**
  in the fallback chain. That's why an `or-llama-8b` request ($0.08/M output) reserves at
  `gemini-flash-lite`'s $1.50/M rate. Verified live: the 402 message reported a $0.001538
  worst case for exactly this reason.

Over-reserving only delays requests that are close to the cap. Settle returns the difference
right away, so the next request has the budget back.

## Money representation
Integer **nano-dollars** in Redis: `INCRBY` is exact and Lua numbers are exact below 2^53, which
is about $9M. Reservations round *up*, settlements round half-up. `Decimal` everywhere in Python.

## Redis is the hot path; Postgres is the durable ledger
- Every request's actual cost is written to `requests.cost_usd` (ADR 0004).
- On the first request of a month, or if Redis lost the key, `reserve()` gets "spent unknown"
  from Lua. It then **hydrates** from `SUM(cost_usd)` on the **primary** (never the read
  replica: a lagging replica would under-count and let the key overspend) and retries.
  `SET NX` means concurrent hydrations can't clobber each other.
- Settle only increments an *existing* total, so a settle racing a hydration can't create a
  partial total.
- Redis runs with AOF (`appendfsync everysec`), so a Redis restart keeps the totals *and* the
  in-flight reservations, which Postgres doesn't have. Its eviction policy is explicitly
  `noeviction`: the response cache shares this Redis, and an LRU policy could silently evict a
  reservation under memory pressure.

**Known gap:** log rows are written asynchronously and can be dropped under extreme pressure
(ADR 0004). If Redis *also* loses its data at that moment, hydration under-counts by those rows.
That needs two simultaneous failures, it's bounded, and it's visible in
`gateway_request_logs_dropped_total`.

## Failure mode
Redis or the ledger is unavailable → **503 (fail closed)**. A spend cap exists to bound money;
silently disabling it during an outage defeats its purpose.

### Settle must not lose the cost (fixed after M4)
If settle fails *after* a response was served, we never fail the response. The first version
then just logged a warning and said "the cost is in Postgres". **That was wrong.** Postgres only
helps when the Redis total is *missing* and gets rebuilt. Here the total still existed, so the
reservation expired a few minutes later and the request's cost silently never counted against
the cap.

The fix has two parts:
- **Settle is idempotent.** The Lua script adds the cost only in the same step that removes the
  reservation. A retry after an ambiguous failure (Redis ran the script, but the reply timed
  out) finds no reservation and does nothing, so the cost can't be counted twice.
- **Failed settles retry in the background** (~35 s of backoff), so the response isn't held.
  While retrying, the reservation, which is the worst case and so ≥ the actual cost, keeps that
  budget held. The cap is safe the whole time.

This relies on one invariant: **a reservation must outlive its request plus the retry window**.
Otherwise a slow request's reservation could expire before it settles. `Settings` refuses to
start unless `spend_reservation_ttl_s >= total_timeout_s + 60`.

If every retry fails (Redis down for more than ~35 s), the cost is lost from the Redis total.
`gateway_spend_settle_failures_total` counts it, and at that point new requests are already
failing closed because reserve() needs Redis too. Pending retries get a last chance on graceful
shutdown.
