# 0007: Failure modes per dependency (fail open vs fail closed)

**Status:** living document; updated as each M2 part lands.

The question for each dependency is: *if it's down, is it worse to keep serving or to stop?*

| Dependency down | Behaviour | Why | Status |
|---|---|---|---|
| Redis: circuit breaker | **fail open**: allow traffic | Optimization only. Retries and fallback still work, just slower during outages. | done (M2a), tested live |
| Redis: response cache | **fail open**: skip the cache (and coalescing), call the provider | A cache miss costs money and latency, not correctness. | done (M2b), tested |
| Redis: rate and spend limiters | **fail closed**: 503. Keys with no limits never touch Redis and keep working. | Overspend is worse than downtime. A budget cap that silently stops working is a financial risk. | done (M2b), tested |
| Postgres when hydrating a month's spend | **fail closed**: 503 | Can't know how much was spent, so we can't safely admit spend. | done (M2b) |
| Postgres primary | keep serving: known keys via stale-if-error cache, unknown keys 503; logs retried and buffered in a bounded queue, dropped with a metric when full | Logs are analytics, not money (Redis holds the spend state). | done (M2c), tested live; ADR 0011 |
| Postgres read replica | dashboard falls back to the primary | Dashboard reads are optional load on the primary, not a correctness need. | done (M2c), tested live |
| One gateway replica | nginx stops routing to it (passive checks); only requests that never reached it are retried | Replicas are stateless; all shared state is in Redis/Postgres. | done (M2c), load-tested |
| Upstream provider | retry, then fallback chain, then breaker | See 0005 and 0006. | done (M2a) |

## Readiness must not depend on shared dependencies
`/readyz` returns 503 **only for replica-local reasons**: currently while draining during
shutdown, so nginx stops sending new requests before the process exits. Redis and Postgres status
appear in the response body as `degraded`, but never fail readiness.

Here's why. Every replica shares the same Redis. If readiness failed on "Redis down", the load
balancer would eject *every* replica at the same moment. A dependency outage that the code is
designed to survive (breaker fails open, cache fails open) would become a total outage.
Readiness answers "should traffic go to *this* replica instead of the others?" For a shared
dependency, the answer is the same for all replicas, so it carries no information.

I first wrote `/readyz` to fail on Redis and found this problem while testing the breaker's
fail-open path for real (`docker compose stop redis`).

`/healthz` is liveness only: the process is up. It never checks dependencies, because restarting
a container doesn't fix a dead Redis.
