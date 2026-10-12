// k6 load test for POST /v1/chat/completions at a constant arrival rate.
//   RATE (req/s), DURATION (e.g. "20s"), KEY (gateway key), URL (default http://nginx:8000),
//   STREAM ("1" for streaming), OUT (summary JSON path)
// Gateway overhead comes from the gateway's own Server-Timing header (total minus time
// waiting on the upstream), so it doesn't depend on the mock's latency.
import http from "k6/http";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

const gatewayOverhead = new Trend("gateway_overhead_ms", true);
const upstreamTime = new Trend("upstream_ms", true);
// 503s from load shedding: the replica refused the request instead of queueing it.
const shed = new Counter("shed");

const RATE = parseInt(__ENV.RATE || "100");
const URL = __ENV.URL || "http://nginx:8000";
const STREAM = __ENV.STREAM === "1";

export const options = {
  discardResponseBodies: !STREAM,
  scenarios: {
    load: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.DURATION || "20s",
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(200, RATE * 4),
    },
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  // A (always-passing) threshold makes k6 export the sub-metric: latency of successful requests
  // only, so fast 503s from shedding don't flatter the latency numbers.
  thresholds: { "http_req_duration{expected_response:true}": ["max>=0"] },
};

const params = {
  headers: { Authorization: `Bearer ${__ENV.KEY}`, "Content-Type": "application/json" },
  timeout: "30s",
};

export default function () {
  // temperature 0.7 + unique content: never a cache hit, so every request takes the full path
  // (auth, rate limit, spend reserve/settle, breaker, upstream call, log).
  const body = JSON.stringify({
    model: "gpt-4o-mini",
    temperature: 0.7,
    max_tokens: 64,
    stream: STREAM,
    messages: [{ role: "user", content: `load test ${__VU}-${__ITER}` }],
  });
  const res = http.post(`${URL}/v1/chat/completions`, body, params);
  check(res, { "status 200": (r) => r.status === 200 });
  if (res.status === 503) shed.add(1);
  const timing = res.headers["Server-Timing"];
  if (timing) {
    const gw = /gateway;dur=([\d.]+)/.exec(timing);
    const up = /upstream;dur=([\d.]+)/.exec(timing);
    if (gw) gatewayOverhead.add(parseFloat(gw[1]));
    if (up) upstreamTime.add(parseFloat(up[1]));
  }
}

export function handleSummary(data) {
  return { [__ENV.OUT || "/results/summary.json"]: JSON.stringify(data) };
}
