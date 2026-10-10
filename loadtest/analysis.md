## Analysis

### Headline
- **Gateway overhead: ~3.7 ms p50, ~6 ms p99** at moderate load, for the full path (auth, rate
  limits, spend reserve and settle, breaker, logging). Against a real LLM call (hundreds of ms to
  seconds) that's ~1–3% added latency.
- **Throughput: 80 → 145 → 190 RPS** for 1 → 2 → 3 replicas (1.81×, 2.38×) on one laptop.
- **0 errors** at every sustained step, and **0 failures** when a replica is killed under load
  (after the two nginx fixes below).

### Where the bottleneck is
At every saturated step **each gateway replica's own CPU is at ~98%** (one uvicorn process = one
event loop = one core), while the shared dependencies have plenty of headroom:

| At 3 replicas, saturated | CPU (100% = 1 core) |
|---|---|
| each gateway replica | ~98% |
| k6 (load generator) | ~50% |
| Redis | ~20% |
| Postgres primary | ≤ 27% (peak across steps) |
| nginx | < 10% |

So capacity is **gateway CPU per request**, not Redis, Postgres or nginx. Past saturation the
gateway doesn't error; latency explodes from queueing (p99 jumps from ~150 ms to 8–14 s). See
"what I'd change" for load shedding.

### CPU per request, and what I did about it
Measured from the container's cgroup CPU counter over 1,000 requests at 50 RPS:

| Request | CPU / request |
|---|---|
| `GET /healthz` (framework floor) | 1.4 ms |
| `GET /v1/models` (auth + JSON) | 2.2 ms |
| chat, key without limits | 9.9 ms |
| chat, key with RPM + TPM + spend cap (before) | **12.8 ms** |
| chat, key with all limits (after the changes below) | **~10.0 ms** |

A cProfile run *inside the container*, grouped by library, showed **our own code is ~4%** of the
CPU. The rest is redis-py, httpx, asyncio and socket syscalls: the cost of each network round
trip in a Python async stack. So the lever was fewer round trips, not faster code:
1. **Circuit breaker:** cache "closed" locally for 1 s (another replica opening the circuit is
   noticed within 1 s), and report successes to Redis only for half-open probes (a success on a
   closed circuit changes nothing). Redis round trips per request went from **6 to 4**.
2. **Request log:** Postgres `COPY` instead of an ORM bulk insert.
3. **Responses:** serialize once, in pydantic-core, instead of dict → `json.dumps`.

Net: **~22% less CPU per request.** (Change 1 was most of it. I measured after each change; 2
and 3 were within noise on their own.)

### Why scaling is sub-linear here
CPU per request *rises* with more replicas: ~9.9 ms with 1 replica, ~12.4 ms with 3 (from
`docker stats`). Everything runs on one machine in Docker Desktop's VM:
- 16 *logical* CPUs are hyperthreads, so 3 busy replicas plus k6 plus Postgres compete for
  physical cores and caches.
- k6 itself burns ~half a core at 200 RPS.
- Docker Desktop's virtualized networking makes every syscall expensive. A bare FastAPI
  `/healthz` costs 1.4 ms of CPU here, about 5× what it costs on bare-metal Linux, while
  pure compute in the container is as fast as the host.

So the **absolute** numbers are pessimistic, and the shape is the useful part: replicas add
capacity, and the shared tier (Redis ~20%, Postgres ≤ 27%) isn't the limit yet. With dedicated
cores per replica, I'd expect close-to-linear scaling until Redis saturates. Redis is
single-threaded at ~20% of a core for ~200 RPS × 4 calls, so its ceiling is roughly 1k RPS on
this hardware.

### Losing a replica: two bugs the load test found
The first failover run (3 replicas, one stopped mid-test) lost 0.1% of requests in two different
ways:
1. **30 s hangs.** A stopped container's IP stops answering instead of refusing, and nginx's
   default `proxy_connect_timeout` is **60 s**, longer than the client's 30 s timeout. Fix: 1 s
   connect timeout, after which nginx retries another replica.
2. **"Connection reset" 502s.** uvicorn closes idle keep-alive connections after **5 s** by
   default, but nginx reuses them for **60 s**. nginx can send a request on a connection the
   gateway just closed. It correctly won't retry a POST it already sent (it might be a
   duplicate, billed LLM call), so the client gets an error. This race also happens during
   normal operation, not only on shutdown. Fix: the gateway's keep-alive (75 s) must be longer
   than nginx's (60 s).

After both fixes: **100.00%** success through the replica kill.

### What I'd change for production
- **Workers per container = cores** (`uvicorn --workers N`, or one process per container with
  more containers). One event loop per core is the scaling unit.
- **Load shedding:** cap in-flight requests per replica and return 503 + `Retry-After` past
  that, instead of letting latency grow to seconds.
- **Pipeline the release phase** (spend settle + TPM true-up in one Redis round trip: 4 → 3).
- **Redis Cluster** past ~1k RPS: every key is already hash-tagged per API key or provider
  (`{...}`), so each Lua script touches a single slot and shards cleanly.
- **Active health checks / LB deregistration before SIGTERM** (Kubernetes readiness + preStop),
  instead of nginx's passive checks.
