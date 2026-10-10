# 0011: Read replica, gateway replicas, and surviving a database outage

**Status:** accepted (M2c). Code: `gateway/db/repositories.py` (`SqlUsageReader`),
`gateway/core/auth.py` (`CachedKeyStore`), `gateway/observability/request_log.py`,
`docker-compose.yml`, `docker/`.

## Postgres: one primary, one streaming read replica
The replica is cloned with `pg_basebackup -R` and streams WAL asynchronously
(`docker/postgres/replica-entrypoint.sh`).

| Query | Goes to | Why |
|---|---|---|
| Request log inserts, key create/update | primary | writes |
| API-key lookup (cache miss) | primary | a replica lagging behind a revocation would let a revoked key back in |
| Spend hydration (`SUM(cost_usd)` for the month) | **primary, never the replica** | a lagging replica under-counts spend, so a key could overshoot its cap. Money needs read-your-writes. |
| `/admin/usage` dashboard | **replica**, falling back to the primary | heavy aggregates (percentiles over a month of rows) shouldn't compete with the write path |

### Why stale dashboard numbers are fine (and where they wouldn't be)
Async replication lag is normally milliseconds, and seconds under heavy write load. A dashboard
that's a few seconds behind is indistinguishable from one refreshed a few seconds ago. Nobody
makes a decision that depends on the last second of traffic. `/admin/usage` returns `source`
(replica/primary) and `replica_lag_s`, so a UI can show "data as of N seconds ago".

Lag would **not** be acceptable for anything that enforces limits or reads its own writes:
spend caps, key revocation, "show me the key I just created". All of those use the primary
(or Redis).

`replica_lag_s` is `now() − pg_last_xact_replay_timestamp()`. When the primary is idle there's
nothing to replay, so this *overstates* lag. That's acceptable for a staleness banner, where an
upper bound is what you want.

### Replica down
Usage queries catch connection errors and time out after 3 seconds, then rerun on the primary
(`gateway_replica_fallbacks_total`). `/readyz` reports `degraded` but stays 200 (ADR 0007).
*Tested live:* stop the replica → `source: primary`; start it → `source: replica`.

## Postgres primary down: keep serving
The question is which requests can still be answered correctly without the database.
- **Known keys keep working.** `CachedKeyStore` is *stale-if-error*: if the lookup fails, an
  entry that expired less than 1 hour ago is still served. Revocation staleness grows from 60s
  to the outage length. That's the right trade here: rejecting every customer during a DB blip
  is worse than honouring a revocation a few minutes late. (Spend caps are in Redis and still
  enforced.)
- **Unknown keys get 503, not 401.** We can't tell "invalid" from "can't check". A 401 would
  tell a valid client its key is wrong.
- **Request logs buffer.** The writer retries the current batch with backoff (0.5s doubling to
  10s) while new records queue in the bounded in-memory queue (10k). When the queue is full,
  new records are dropped and counted. Non-transient errors (bad data) drop the batch
  immediately, so one poison batch can't block logging forever.
- *Tested live:* `docker compose stop postgres` → chat still 200, an unknown key gets 503, the
  backlog metric rises; after `start`, all 4 buffered rows land in the table (30 → 34).

**Why not buffer in Redis instead of memory?** It would survive a gateway crash, but it adds
Redis writes to every request to protect *analytics*. The bounded memory buffer covers outages
of minutes, and settled spend (the thing that matters) lives in Redis already (ADR 0009).

## Gateway replicas behind nginx
- `docker compose up -d --scale api=3`, with nginx round-robin over Docker DNS. Replicas are
  stateless: everything shared (limits, spend, breaker, cache) lives in Redis or Postgres. A
  request can land on any replica, and losing one loses no state.
- **Passive health checks:** open-source nginx has no active health probes. `max_fails=1
  fail_timeout=5s` takes a replica out after a connection failure. `proxy_next_upstream error
  timeout` retries **only requests that never reached the replica**. nginx never re-sends a POST
  that was already sent, so a replica dying mid-request can't cause a duplicate (double-billed)
  upstream call.
- **Graceful shutdown:** uvicorn finishes in-flight requests on SIGTERM
  (`--timeout-graceful-shutdown 30`), and `/readyz` returns 503 while draining.
- **Migrations run once**, in a `migrate` one-shot service that the API waits on
  (`service_completed_successfully`). With migrations in each replica's start command, three
  replicas starting together would race to create the same tables.
- nginx resolves `api` when it starts, so it must be restarted after scaling
  (`loadtest/run.py` does this). In Kubernetes, a Service plus readiness probes replaces all of
  this.
