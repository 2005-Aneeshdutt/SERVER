# 0004: Request logs are written off the hot path and dropped under pressure

**Status:** accepted (M1). M2 revisits the "Postgres down" case.

## Decision
Handlers call `RequestLogWriter.submit()`, which is a non-blocking `put_nowait` into a bounded
in-process queue (default 10k). One background task drains the queue. Each insert takes
*everything that has accumulated* since the last one, up to 200 rows, in a single `executemany`.

- **Queue full:** drop the record and increment `gateway_request_logs_dropped_total{reason="queue_full"}`.
- **Insert fails:** drop the batch and increment `{reason="db_error"}`. No retry loop.
- **Shutdown:** drain for up to 5s.

## Why
- **Latency:** the client's response never waits on Postgres. Logging is roughly 1µs per request.
- **Natural batching:** there's no flush timer and no added delay. When idle, batches have size 1.
  Under load, batches grow automatically, so the number of DB round trips grows more slowly than
  request volume.
- **Bounded memory:** a slow or dead database can't make the gateway run out of memory.

## Tradeoff
Logs are best-effort. A crash loses whatever is still queued, which is at most a second or so of
records under normal load. That's acceptable because these rows feed dashboards and analytics.
**Money is not enforced from these rows:** spend caps are enforced from Redis (reserve, then
settle; ADR 0009). The rows are the *durable ledger* that Redis's monthly total is rebuilt from
if Redis loses it. A dropped row therefore only matters if Redis also loses its data at the same
time. That gap is bounded and documented in ADR 0009, and the drop counter makes any loss visible
instead of silent.
