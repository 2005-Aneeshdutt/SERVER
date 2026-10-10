# 0006: Per-provider circuit breaker, state in Redis

**Status:** accepted (M2a). Code: `gateway/routing/breaker.py`, `gateway/routing/lua/`.

## Why have a breaker at all (we already retry)
Retries handle *blips*. When a provider is down for minutes, retries make things worse: every
request burns 2 attempts plus backoff (up to a few seconds of added latency) before failing over,
and we keep hammering a struggling provider. A breaker remembers that the provider is down and
sends traffic straight to the fallback, in 0 attempts.

## State machine
```
closed  --5 failures within 30s-->  open  --30s cooldown-->  half-open
half-open: exactly ONE request (the probe) is let through
           success -> closed         failure -> open for another 30s
```

## Why the state lives in Redis
With 3 replicas and per-process state, each replica would need to discover the outage on its own
(3× the failed requests), and they'd disagree about whether the provider is up. With shared
state, one replica's failures protect all of them.

## Why Lua, and why Redis's clock
- **Atomicity:** "count failures, compare to the threshold, open the circuit" must be a single
  step. As separate commands, two replicas could both read 4 failures, both add one, and the
  check would be lost. A Lua script runs atomically in Redis.
- **The half-open probe** uses `SET probe NX PX` inside the script, so among any number of
  concurrent requests on any number of replicas, exactly one gets through.
  *Tested: 50 concurrent `allow()` calls over 5 connections, exactly 1 was allowed.*
- **Time** comes from `redis.call('TIME')`, not from the replicas. Replica clocks drift, and
  Redis's clock is the single source of truth for "has the cooldown elapsed?"

## Thresholds (configurable via `BREAKER_*` env vars)
- **Window 30s, 5 failures:** a sliding window (a sorted set of failure timestamps), so old
  failures age out. A fixed count would eventually trip on rare, unrelated errors spread over
  hours. 5 failures is high enough that one bad request (or one replica's network blip) doesn't
  trip it. A real outage reaches 5 failures in well under a second at normal traffic levels.
- **Cooldown 30s:** long enough for typical provider incidents (overload, deploys) to clear,
  short enough that we recover quickly. It costs one probe request per 30s while the provider is
  down.
- **Probe lock TTL 15s:** if the replica holding the probe crashes, another probe is allowed
  after 15s instead of the circuit staying stuck.
- **Idle reset 10 min:** an open circuit that nobody probes (no traffic) resets to closed.

**What counts as a failure:** 5xx, timeouts, connection errors, 429 and upstream auth errors.
Client `BadRequest`s don't count, because a user sending bad tool schemas must not trip the
breaker for everyone. A 429 *does* count: it means "stop sending", and that's exactly what an
open circuit does.

## If Redis is down: fail OPEN (allow traffic)
The breaker is an optimization. Without it we still retry and fail over; requests just get a bit
slower during a provider outage. Rejecting all traffic because the breaker's storage is down
would make Redis a hard dependency for no safety gain. Errors are counted in
`gateway_redis_errors_total{component="breaker"}`. Redis calls use 0.5s timeouts, so a hung Redis
can't stall requests. (The spend and rate limiters, coming in M2b, fail *closed* instead; see 0007.)

## Clock steps (found in M2b testing)
Docker Desktop's Redis clock was measured stepping **back by up to ~1s** every few seconds. If
the clock steps back between "probe granted" and "probe succeeded", `now < open_until` again and
a time-only check would ignore the probe's result, leaving the circuit stuck half-open until the
lock expired. Half-open is now detected as *cooldown elapsed **or** probe lock present*, so the
probe's outcome always counts. A backward step otherwise only makes the breaker slightly more
conservative (a longer cooldown), which is safe.
