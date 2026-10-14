"""Prometheus metrics (M1 subset; M2 adds cache, breaker and limiter metrics)."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "gateway_requests_total", "Chat completion requests", ["alias", "provider", "status"]
)
REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "End-to-end request latency",
    ["alias", "stream"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
TIME_TO_FIRST_TOKEN = Histogram(
    "gateway_time_to_first_token_seconds",
    "Time until the first content chunk from upstream",
    ["alias"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
REQUEST_LOGS_DROPPED = Counter(
    "gateway_request_logs_dropped_total", "Request log records not persisted", ["reason"]
)
USAGE_ESTIMATED = Counter(
    "gateway_usage_estimated_total", "Responses whose token usage we had to estimate", ["provider"]
)
UPSTREAM_ATTEMPTS = Counter(
    "gateway_upstream_attempts_total",
    "Calls to upstream providers by outcome (ok, error type, or circuit_open skip)",
    ["provider", "outcome"],
)
FALLBACKS = Counter(
    "gateway_fallbacks_total", "Requests served by a fallback alias", ["requested", "served"]
)
BREAKER_STATE = Gauge(
    "gateway_circuit_state",
    "Last circuit state seen by this replica (0 closed, 1 open, 2 half-open)",
    ["provider"],
)
REDIS_ERRORS = Counter("gateway_redis_errors_total", "Redis calls that failed", ["component"])
CACHE_LOOKUPS = Counter(
    "gateway_cache_lookups_total",
    "Response cache lookups: hit, miss, bypass (not cacheable), coalesced (hit after waiting)",
    ["result"],
)
CACHE_SAVED_USD = Counter("gateway_cache_saved_usd_total", "Upstream cost avoided by cache hits")
CACHE_SAVED_SECONDS = Counter(
    "gateway_cache_saved_seconds_total", "Upstream latency avoided by cache hits"
)
LIMIT_REJECTIONS = Counter(
    "gateway_limit_rejections_total", "Requests rejected by a per-key limit", ["limit"]
)
REQUEST_LOG_BACKLOG = Gauge(
    "gateway_request_log_backlog", "Request log records waiting to be written (incl. retrying)"
)
REQUEST_LOG_RETRIES = Counter(
    "gateway_request_log_retries_total", "Request log batch writes retried (DB unreachable)"
)
KEY_STORE_STALE = Counter(
    "gateway_key_store_stale_total", "API key lookups served from a stale cache entry (DB down)"
)
REPLICA_FALLBACKS = Counter(
    "gateway_replica_fallbacks_total", "Read-replica queries that fell back to the primary"
)
ROUTER_DECISIONS = Counter(
    "gateway_router_decisions_total", "model=auto routing decisions", ["route", "reason"]
)
ROUTER_LATENCY = Histogram(
    "gateway_router_latency_seconds",
    "Time to make a model=auto routing decision (incl. prompt embedding)",
    buckets=(0.002, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.2, 0.5),
)
IN_FLIGHT = Gauge("gateway_in_flight_requests", "Requests this replica is currently serving")
REQUESTS_SHED = Counter(
    "gateway_requests_shed_total", "Requests rejected with 503 because the replica was at capacity"
)
SPEND_SETTLE_FAILURES = Counter(
    "gateway_spend_settle_failures_total",
    "Served requests whose cost never reached the Redis spend total (settle retries exhausted)",
)
CAPTURE_SENT = Counter("gateway_capture_sent_total", "Capture events handed to Kafka")
CAPTURE_DROPPED = Counter(
    "gateway_capture_dropped_total",
    "Capture events dropped (capture fails open: requests are never affected)",
    ["reason"],
)
CAPTURE_BACKLOG = Gauge("gateway_capture_backlog", "Capture events waiting in the in-process queue")
DISTILL_REQUESTS = Counter(
    "gateway_distill_requests_total",
    "Distilled-model rollout events: distilled, failed, rolled_back, shadow, shadow_skipped",
    ["task", "event"],
)
