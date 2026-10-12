# 0014: Load shedding with a per-replica in-flight cap

**Status:** accepted. Code: `gateway/api/load_shed.py`, `gateway/app.py`,
`loadtest/run.py` (`overload_test`, `cap_study`). Results: `loadtest/RESULTS.md`.

## The problem
The first load test found that past saturation the gateway didn't fail, it **queued**.
One replica tops out at ~80 RPS. When offered more, it accepted every request anyway, and:
- successful requests took **19–28 s at p99** (they were fine at 0.16 s just below saturation);
- **goodput fell to ~47–50 RPS**, *below* the 80 the replica can serve. The replica spent its
  CPU on requests whose clients had already timed out (k6 waits 30 s);
- k6 couldn't even send 800–2,300 requests per step, because all its connections were stuck
  waiting.

This is the classic overload collapse. The queue grows without limit, and every request
in it gets slower, including the ones that will eventually succeed.

## Decision
Each replica counts its in-flight requests. Once the count reaches `MAX_IN_FLIGHT`, it answers
new requests immediately with **`503` + `Retry-After: 1`** and an OpenAI-shaped error
(`code: "overloaded"`), so SDKs treat it as a retryable server error.

Details that matter:
- **Pure ASGI middleware, not `BaseHTTPMiddleware`.** It adds no extra task per request.
  It also releases the slot only when the *response body* finishes, so a long stream holds its
  slot for its whole life. There's a unit test for exactly that.
- **`/healthz`, `/readyz` and `/metrics` are never shed.** If health checks failed under load,
  nginx would eject the busy replica and push its traffic onto the others, cascading the
  overload. And `/metrics` is how you'd see the overload in the first place.
- **Overload does not fail readiness**, for the same reason.
- **Counter, not semaphore-with-wait.** Waiting for a slot is just a queue with extra steps.
  The point is to say no *now*.
- **A plain `int`, no lock.** One uvicorn process is one event loop. The check and the increment
  happen with no `await` in between, so no other request can interleave.
- **Metrics:** `gateway_requests_shed_total` and `gateway_in_flight_requests`.

### Why not uvicorn's `--limit-concurrency`?
It does almost the same thing, but:
- it counts health checks too, which leads to the cascade above;
- it sends a bare 503 with no `Retry-After` and no OpenAI error body;
- it exports no metric.
Fifty lines of our own middleware fix all three.

### Why not shed at nginx?
nginx's `limit_conn` caps connections per nginx instance, not work per gateway replica. It
can't tell a cheap `/v1/models` call from a chat completion, and it would need re-tuning
whenever replicas are added. The replica knows its own capacity best.

## Result (1 replica, same offered load, from `loadtest/RESULTS.md`)

| Offered | Shedding | Goodput | OK p50 / p99 | Shed | Dropped by k6 |
|---|---|---|---|---|---|
| 100 RPS | off | 48.6 | 4.5 s / 19.1 s | 0% | 787 |
| 100 RPS | on (16) | **90.4** | 0.15 s / **0.29 s** | 8% | 0 |
| 200 RPS | off | 49.3 | 8.7 s / 28.3 s | 4% | 2,299 |
| 200 RPS | on (16) | **76.0** | 0.20 s / **0.47 s** | 62% | 0 |

Shedding didn't just bound latency, it **raised goodput by 55–85%**. The CPU now goes to requests
that will be answered in time, instead of to requests whose clients have given up.

## Choosing the cap: Little's law
Concurrency = throughput × latency. One replica at saturation serves ~80 RPS at ~0.125 s, so
**~10 requests are in flight when it's fully busy**. The cap must sit a bit above that:
- **Too low** (8): the replica sits idle waiting for work it turned away. Goodput was ~60 RPS
  while it can do 80+.
- **Too high** (64): the queue in front of the CPU is back. p99 grows to ~4 s and goodput drops
  to 57–65 RPS. 32 is in between (p99 1.4–1.6 s).
- **16 (~1.5×)**: goodput stays at roughly the replica's full capacity, and the p99 of successful
  requests stays well under 1 s.

Each cell is a single 20 s run on a laptop that's also running k6, Postgres and Redis, so single
cells are noisy (16 @ 130 RPS gave p99 0.9 s in the cap study but 0.37 s in the main run). The
ordering of the caps is consistent across both runs, though.

The full table is in `loadtest/RESULTS.md` ("Choosing the cap"), reproducible with
`uv run python loadtest/run.py --overload-only`.

### The catch: the right cap depends on upstream latency
The load test's mock answers in 100 ms. Real LLM calls take 1–10 s, and while waiting on them a
request uses no CPU. With a 3 s average upstream, ~80 RPS of CPU capacity means **~240 requests
in flight** at saturation. A cap of 16 would shed almost all real traffic. So:
- the overload test sets `MAX_IN_FLIGHT=16`, matching the mock. The scaling sweep keeps 512, so
  it measures raw capacity;
- the production default is **512**, sized for multi-second upstream latency (≈2× the 240 above).

A static concurrency cap is the simplest thing that works, and its weakness is that it has to
be tuned to the latency distribution. **What I'd do next:**
- an **adaptive limit**: AIMD or gradient-based, like Netflix's `concurrency-limits`, which lowers
  the cap when latency rises above its baseline;
- or shedding on **queueing delay** (event-loop lag), which measures the real problem (CPU
  contention) directly.
Both are more moving parts than this project needs to make the point.

## What it costs
Nothing measurable below the cap. The check is an integer compare per request. Past the cap,
each rejection still costs the replica a little CPU (parse the request, send 65 bytes), so
goodput under extreme overload (200 RPS offered, 2.5× capacity) is ~76 RPS rather than the ~90
peak. That's still far better than the ~49 RPS collapse without shedding.

## Tradeoffs and what a client sees
- A shed request fails fast instead of succeeding slowly. For an interactive app that's the
  right call: the user's request would have timed out anyway. The client (or another replica,
  via the load balancer on retry) can try again in a second.
- nginx does not retry 503s automatically. Retrying a POST is only safe here because a shed
  request never reached the handler, but making nginx retry *only* those would need
  `non_idempotent` retries for all errors, which risks double-billing a real LLM call. So the
  retry is left to the client's `Retry-After` handling, which the OpenAI SDKs already do for 503.
