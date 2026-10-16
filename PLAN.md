# PLAN: closed-loop distillation (M5–M12)

Status: **paused after M5-M12 were built** (ADR 0015, ADR 0016). M5-M10 ran end to end on simulated traffic (results: `distill/RESULTS.md`; no model has passed the gate yet). M11 (pipeline) and M12 (drift demo, sampled-audit rollback) are built and tested, not yet run on full data. Remaining runs: see README "Future work".

The goal: the gateway mines its own traffic and finds narrow, repeated tasks sent to an
expensive model. For each one it:
1. distills it into a small fine-tuned model;
2. proves that model is good enough with statistical gates;
3. rolls it out shadow → canary → full, with automatic rollback.

---

## 0. What I verified in the repo (and two corrections)

Everything in the brief checks out against the code:
- **Gateway:** stateless FastAPI replicas behind nginx.
- **Redis:** Lua scripts for the token-bucket rate limits, reserve-then-settle spend, the cache
  with coalescing, and the circuit breaker.
- **Postgres:** a primary plus a streaming read replica.
- **Routing:** `models.yaml` with fallback chains, plus `model: "auto"`.
- **The M3 negative result is real:** classifier APGR 0.537 vs heuristic 0.532, a difference of
  +0.005 with a 95% CI of −0.028 to +0.037 (`eval/RESULTS.md`).
- **Tests:** 157 unit + 34 integration tests, and k6 load tests.

Things the brief doesn't mention, which the plan has to respect:

| Fact in the code | Why it matters here |
|---|---|
| `CLAUDE.md` lists **fine-tuning as a non-goal** and says *"do not add dependencies outside this list without asking"* | This project changes both. §7 lists every new dependency for approval, and `CLAUDE.md` gets updated in M5. |
| `chat._finish()` runs **before** a non-streaming response is returned. Streams build a full response only when they lead a cache miss (`acc.to_response` only if `lock_token`). | Capture must be a non-blocking `put_nowait` inside `_finish`. Streams need the accumulated text even without the cache. |
| `Dispatcher.candidates()` filters by `key.may_use(alias)` | A request diverted to `distilled/<task>-v1` would be filtered out for any key with `allowed_models` (e.g. demo keys). The rollout path needs an explicit rule (§4.4). |
| Load testing showed that every Redis round trip costs about 0.5 ms of CPU, and the breaker became cheaper once it cached state locally (ADR 0006) | Rollout state must **not** cost a Redis call per request. It's cached per replica, like the breaker. |
| `strong` models here are cheap in absolute terms (gpt-oss-120b at $0.15/$0.60 per 1M tokens) | Savings in **%** will be large but in **$** small. The results table reports both, priced from `models.yaml`. |
| ~$0.28 is left on OpenRouter. Groq and Gemini are on free tiers. | Teacher labelling must run on free tiers with caching. Dataset sizes are planned around rate limits (§6, risk R1). |
| Docker Desktop VM: 8 GB | Kafka, the consumer and llama.cpp fit only with explicit heap and memory limits (risk R5). |

---

## 1. Architecture at a glance

```
                       hot path (flags default OFF)                       offline / async
client ─► nginx ─► gateway ──────────────────────────────► provider
                     │  match task (in-memory fingerprint)
                     │  rollout arm? strong | distilled | shadow
                     │     └─ distilled ─► llamacpp container (OpenAI-compatible)
                     │  _finish: capture.submit()  (put_nowait, bounded, fails open)
                     ▼
              CaptureProducer task ── scrub PII ── aiokafka ─► Kafka (1 broker, KRaft)
                                                               │ topic gateway.events.v1
                                                               │ topic distill.shadow.v1
                                         ┌─────────────────────┴───────────────┐
                                  capture-consumer                       (replayable)
                              Postgres metadata + Parquet content
                                         │
   distill CLI:  discover ─► build dataset ─► [train on Kaggle/Colab] ─► eval gate ─► rollout
                 (embed+cluster) (2 teachers,      (one command,        (bootstrap   (Redis state,
                                  validators,       GGUF out)            NI test)     audit table)
                                  dedupe, split)
```

**Why Kafka here, when I advised against it for request logs:**
- Payloads (full prompts and responses) are large, and several independent readers need them:
  - the Postgres/Parquet writer;
  - shadow comparison;
  - drift monitoring;
  - dataset rebuilds.
- A dataset must be **re-buildable by replaying the topic** after a validator or scrubber
  changes.

That's fan-out plus replay, which is what a log is for. Single-broker means **no durability
claim**: capture is best-effort by design (it fails open), and ADR 0015 says so.

**Code layout.** Hot-path code stays small and lives under `gateway/`. Everything offline lives
in a new top-level package, `distill/`, so the gateway image and its tests don't grow with
training concerns.

---

## 2. Files I'll touch

### Existing files (all changes behind flags that default OFF)
| File | Change |
|---|---|
| `gateway/core/config.py` | New settings: `capture_enabled`, `kafka_bootstrap`, `capture_queue_size`, `capture_topic`, `distill_rollout_enabled`, `distill_shadow_enabled`, `distill_shadow_concurrency`, `llamacpp_base_url`, rollback thresholds |
| `gateway/api/chat.py` | (a) After `_route`, before `_reserve`: `rollout.choose()` may swap `call.trace.spec`. (b) In `_finish`: `capture.submit(...)`. (c) Streams keep the accumulated text when capture is on. (d) Shadow call scheduled after the response. |
| `gateway/container.py` | Wire `CaptureProducer`, `TaskMatcher`, `RolloutController`, and closers |
| `gateway/app.py` | Start/stop the capture producer in the lifespan, as `request_log` is today |
| `gateway/routing/registry.py` | Add `llamacpp` to `KNOWN_PROVIDERS` |
| `gateway/providers/factory.py` | `LlamaCppProvider` when `LLAMACPP_BASE_URL` is set |
| `gateway/db/models.py` | New tables (§3.3), plus `api_keys.distill_opt_in` and `api_keys.simulated` |
| `gateway/core/auth.py`, `gateway/db/repositories.py`, `gateway/api/admin.py` | Expose the two key flags; new admin endpoints `GET /admin/distill/tasks`, `GET /admin/distill/audit`, `POST /admin/distill/tasks/{id}/rollout` |
| `gateway/observability/metrics.py` | Capture dropped/queued/sent counters; rollout arm counters; validator failures; shadow agreement |
| `docker-compose.yml` | Services `kafka`, `capture-consumer` and `llamacpp`, **all under profile `distill`**, so `docker compose up` is unchanged |
| `loadtest/*` | A `--capture` variant of the overhead run (hard rule 2) |
| `web/src/components/Dashboard.tsx`, `web/src/api.ts` | A "Distillation" panel: tasks, state, weight, failure rate, audit log, with a "simulated" badge |
| `.github/workflows/ci.yml` | Integration job also runs Kafka (service container) for the capture tests |
| `README.md`, `CLAUDE.md`, `.env.example` | Docs, the scope change, new env vars |

### New files
```
gateway/capture/producer.py      bounded queue -> background task -> scrub -> aiokafka; fails open
gateway/capture/events.py        Pydantic event schema (versioned)
gateway/capture/pii.py           email / phone / card-number (Luhn-checked) scrubbing
gateway/capture/fingerprint.py   normalized system-prompt + template skeleton hash
gateway/distill/matcher.py       in-memory task table (refreshed from Redis every few s)
gateway/distill/rollout.py       arm selection, local state cache, online validators, counters
gateway/distill/lua/rollout_record.lua   atomic window counters + rollback check
gateway/distill/validators.py    json-schema / label-in-set / sql-parses-and-executes
gateway/providers/llamacpp.py    OpenAI-compatible adapter (subclass, like ollama.py)
migrations/versions/0003_distillation.py

distill/                         offline package (not imported by the gateway)
  consumer.py        Kafka -> Postgres (metadata, ON CONFLICT DO NOTHING) + Parquet (content)
  replay.py          traffic replayer: public datasets -> gateway, keys flagged simulated
  datasets/          loaders for the 3 tasks + background "noise" traffic
  discover.py        embed -> cluster -> score (volume, stability, checkability)
  build.py           2 teachers -> validators -> agreement -> dedupe -> split -> hash
  eval.py            task metrics, paired bootstrap, non-inferiority decision
  judge.py           LLM judge (reuses eval/judge.py's position-bias handling)
  label_cli.py       terminal labeling tool for ~200 human labels -> Cohen's kappa
  pipeline.py        discover -> build -> (train handoff) -> eval -> shadow -> canary -> promote
  drift.py           drift injection + timeline report
  bench_serving.py   TTFT / tokens/s / memory: llama.cpp (cache on/off) vs API model
train/
  train_lora.py      one command on Kaggle/Colab: LoRA (Unsloth) -> merged -> GGUF
  requirements.txt   pinned; NOT part of the uv env (GPU-only)
  kaggle.ipynb       thin wrapper that runs train_lora.py
docker/llamacpp/     server flags (prompt cache, slots), model volume
docs/decisions/0015-capture-via-kafka.md, 0016-task-discovery.md, 0017-distill-eval-gate.md,
  0018-shadow-canary-rollback.md
tests/unit/test_pii.py, test_fingerprint.py, test_capture_producer.py, test_rollout.py,
  test_validators.py, test_distill_eval.py, test_discover.py, test_build.py
tests/integration/test_capture_kafka.py, test_rollout_redis.py, test_consumer_idempotent.py
results/ (generated): distill/RESULTS.md, serving/RESULTS.md, drift/RESULTS.md
```

---

## 3. Data schemas

### 3.1 Kafka event: `gateway.events.v1`

_Changed in M5: events are batched, up to 200 per Kafka message, as JSON lines, with no message
key. One message per event, keyed by API key, cost too much CPU in the Kafka client. Per-key
ordering in Kafka isn't needed downstream. See ADR 0015._
```json
{
  "schema_version": 1,
  "event_id": "uuid (= request id; consumers dedupe on it)",
  "ts": "2026-10-13T10:00:00.123Z",
  "simulated": true,
  "key_id": "uuid",
  "content_captured": true,
  "request": {
    "alias": "groq-gpt-oss-120b",
    "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
    "params": {"temperature": 0, "max_tokens": 256, "response_format": "json_schema|json_object|null",
               "has_tools": false, "stream": false},
    "system_fingerprint": "sha256 of normalized system prompt",
    "template_fingerprint": "sha256 of user-message skeleton (literals masked)"
  },
  "response": {"text": "...", "finish_reason": "stop", "tool_calls": null},
  "meta": {"served_alias": "...", "provider": "...", "upstream_model": "...", "status": "ok",
           "error_type": null, "prompt_tokens": 120, "completion_tokens": 40,
           "cost_usd": "0.0000420000", "latency_ms": 812, "ttft_ms": null,
           "cache_hit": false, "fallback_count": 0, "route": null},
  "pii": {"email": 0, "phone": 1, "card": 0},
  "rollout": {"task_id": "uuid", "model_version": "v1", "arm": "strong|distilled"} 
}
```
- `messages` and `response.text` are present **only if** the key has `distill_opt_in = true`
  (hard rule 4). Otherwise `content_captured = false`, and the event carries metadata and
  fingerprints only.
- PII is scrubbed **inside the gateway process, in the producer's background task, before the
  event leaves the process**. That keeps raw content out of Kafka, Postgres and Parquet, and the
  scrubbing CPU off the request path.
- `simulated` is true if the key is flagged `simulated` **or** the request carries
  `X-Gateway-Simulated: 1` (hard rule 5).

`distill.shadow.v1` has the same envelope, plus `{"strong_text", "distilled_text",
"validator": {...}, "agree": bool, "distilled_latency_ms"}`.

### 3.2 Dataset row (Parquet; one file per `task/<dataset_hash>/{train,test}.parquet`)
| Field | Notes |
|---|---|
| `example_id` | sha256 of normalized input |
| `task_id`, `source_event_id`, `simulated` | provenance |
| `messages` | scrubbed chat input, exactly as served |
| `target` | teacher A output: **the training label** |
| `teacher_a`, `teacher_b`, `teacher_b_output`, `teachers_agree` | agreement filter |
| `validator`, `validator_passed`, `validator_detail` | e.g. schema-valid, label in set, SQL executes |
| `gold` | dataset ground truth, **eval only, never trained on** (used to measure the teachers too) |
| `split` | `train` or `test`. The split is done **after** exact and near-duplicate dedupe, by `example_id` hash, so it's stable. |

Dataset-level `manifest.json`: `dataset_hash` (sha256 over sorted rows), counts at each filter
stage, teacher models, validator versions, seed. Training logs this hash, and eval refuses to run
on a mismatched pair.

### 3.3 New Postgres tables (migration `0003`)
| Table | Key columns |
|---|---|
| `capture_events` | `id` (PK = event_id, `ON CONFLICT DO NOTHING`), ts, key_id, simulated, alias, served_alias, fingerprints, content_captured, parquet_path, tokens, cost_usd, latency_ms, status, task_id, arm |
| `distill_tasks` | id, name, source_alias, system_fingerprint, centroid (float[]), validator spec (jsonb), discovery scores, status |
| `distill_datasets` | id, task_id, dataset_hash, n per stage, manifest (jsonb) |
| `distill_models` | id, task_id, version, base_model, gguf_sha256, dataset_hash, train_config (jsonb) |
| `distill_evals` | id, model_id, dataset_hash, metric, n, distilled_mean, strong_mean, diff, ci_low, ci_high, margin, passed, judge (jsonb: model, kappa, n_human), per-example results path |
| `distill_audit` | id, ts, task_id, model_version, action (`shadow`, `canary`, `promote`, `rollback`, `manual`), weight_from, weight_to, actor (`auto`, `cli`, `admin`), reason, metrics_snapshot (jsonb) |
| `human_labels` | example_id, model_version, label, labeler, ts |

### 3.4 Eval record (`distill_evals` + JSON artifact)
```json
{"eval_id": "...", "task": "banking77-intent", "model_version": "v1", "dataset_hash": "...",
 "n": "<int>", "metric": "accuracy_vs_gold",
 "distilled": "<float>", "strong": "<float>", "diff": "<distilled - strong>",
 "ci95": ["<lo>", "<hi>"], "bootstrap": {"resamples": 10000, "seed": 7, "paired": true},
 "margin": 0.03, "decision": "pass iff ci95.lo > -margin",
 "judge": null}
```

### 3.5 Rollout state (Redis; Postgres keeps the audit trail)
```
distill:{task}:state  HASH  state=off|shadow|canary|full|rolled_back  model_alias  version
                            weight=0.00..1.00  updated_at  reason
distill:{task}:win:{epoch_minute}  HASH  n, fail   (TTL 1h; summed over the window by Lua)
```
- Each replica caches `state` locally for ~2 s. That's the same idea as the breaker's 1 s
  closed-cache, so there's **no Redis read per request**.
- Counters are written by one Lua script per *distilled* request, never for unaffected traffic.
- That script also checks the rollback rule **atomically**, so two replicas can't both "half
  roll back".

---

## 4. Milestones: what each delivers and how it's tested

Every milestone ends the same way: ruff, mypy --strict, unit and integration tests green; a
results note; a commit; then **I stop for your review**.

### M5: Capture
- **Kafka:** `apache/kafka`, pinned, single node, KRaft, heap capped at 512 MB, under the
  `distill` compose profile.
- **Producer:** `CaptureProducer.submit()` is `put_nowait` on a bounded queue. Full queue → drop
  and count a metric, never block. A background task scrubs, serializes and sends via `aiokafka`
  (`acks=1`, `linger_ms` batching). Kafka down → events are dropped and counted, and requests
  are unaffected (fail open).
- **Consumer:** `distill/consumer.py` writes metadata to Postgres (idempotent on `event_id`) and
  content to Parquet, partitioned by `date/simulated`.
- **Replayer:** `distill/replay.py` drives the gateway with simulated keys at a target RPS.
  - **Proposed datasets.** Licenses get verified before download; if one is unusable I'll
    report it and propose a swap.
    - **Classification:** Banking77 (77 intents), scored as exact match.
    - **Text-to-SQL:** WikiSQL, scored by execution accuracy: tables are loaded into in-memory
      SQLite and gold and predicted results are compared.
    - **JSON extraction:** Few-NERD coarse entities as JSON, scored with entity-set F1.
  - **Noise traffic**, so discovery isn't trivial: M3's 2,000 LMSYS prompts, labelled simulated.
    They're served by the existing **mock LLM** to cost $0, because only the prompts matter for
    clustering.
- **Tests:**
  - **Unit:** the PII scrubber (positives, negatives, Luhn check), fingerprint stability,
    producer queue-full drops, Kafka-down fail-open, opt-out keys carry no content, stream text
    captured.
  - **Integration (real Kafka):** gateway → Kafka → consumer → Postgres rows equal sent events;
    replaying the same events gives no duplicates.
  - **k6:** gateway overhead p50/p99 with capture **off** vs **on** (hard rule 2), same harness
    as `loadtest/RESULTS.md`.
- **Result note:** event counts, drop counts under a Kafka-down test, and the before/after
  overhead table.

### M6: Task discovery and datasets
- **Discovery:** embed prompts with the gateway's own bge-small embedder (no new model), then
  cluster with scikit-learn's HDBSCAN (already in the eval group). Clusters are scored by:
  - **volume:** share of traffic to a *strong* alias;
  - **output stability:** how often teacher outputs agree, plus output-format entropy;
  - **checkability:** whether a validator applies (JSON schema, closed label set, SQL that
    executes).
  - The top-k clusters become `distill_tasks`, each with a `system_fingerprint` matcher and an
    embedding centroid.
- **Builder:**
  - **Teachers:** teacher A is the strong model that served the traffic (`groq-gpt-oss-120b`);
    teacher B is `gemini-flash`. Both run on free tiers, and every call is cached to disk, so
    labelling is paid for once.
  - **Filters:** validator pass, teacher agreement, exact dedupe plus near-duplicate dedupe
    (cosine > 0.95), and a stable hash split.
  - **Gold** is attached for eval only.
- **Tests:**
  - **Unit:** clustering on synthetic, well-separated data recovers the planted clusters; scores
    rank a stable/checkable cluster above noise; validators are correct per task; dedupe removes
    near-duplicates across splits (leakage test); the dataset hash is deterministic.
  - **Integration:** a build on a 50-event fixture, with teachers mocked via respx.
- **Result note:** for each task, the counts at every filter stage, teacher-vs-gold accuracy,
  and teacher agreement rate.
- **I also report whether discovery found the 3 planted tasks among the noise.** It *should*,
  because we planted them, and I'll say exactly that.

### M7: Training (you run the GPU part)
- `train/train_lora.py --task <name> --base qwen2.5-0.5b|qwen2.5-1.5b` does the following:
  - LoRA with Unsloth (PEFT fallback), fixed seeds;
  - exports merged weights and a Q4_K_M GGUF;
  - writes `train_config.json` (hyperparameters, `dataset_hash`, library versions, seed, GPU)
    next to the weights.
- **Handoff:**
  1. `distill pipeline export --task X` writes the dataset bundle and prints the one Kaggle
     command.
  2. You upload the outputs into `models/<task>/v1/`.
  3. `distill models register` checks `gguf_sha256` and `dataset_hash` before accepting it.
- **Tests:** CPU smoke test of the data formatting and chat template on 5 rows. The training loop
  itself can't run in CI and **won't be claimed as tested**. Also a unit test that a dataset-hash
  mismatch is rejected on register.

### M8: Serving
- A `llamacpp` container (`ghcr.io/ggml-org/llama.cpp:server`, pinned build tag) with the GGUF
  mounted, prompt caching on (`cache_prompt`) and parallel slots configured.
- Registered in `models.yaml` as `distilled/<task>-v1`: provider `llamacpp`, fallback = the
  strong source alias.
- `distill/bench_serving.py` measures:
  - TTFT p50/p99 and decode tokens/s;
  - container memory (`docker stats`);
  - distilled with the prompt cache **on vs off**, vs the API model, on the same held-out
    prompts.
- **Expected honesty point:** CPU llama.cpp on a laptop may be **slower** than Groq's hosted
  API. If so, the win is cost and independence, not latency, and the table will show that.
- **Tests:** a provider adapter unit test with respx (OpenAI-compatible); a registry test; a
  failover test (llama.cpp down → strong model serves).

### M9: Eval gate
- **Task metrics** compare against **gold** on the held-out split: accuracy, execution accuracy,
  entity F1. Both the distilled model and the strong model are scored on the same examples.
- **Non-inferiority:** a paired bootstrap of (distilled − strong), 10k resamples, fixed seed.
  It **passes iff the 95% CI lower bound is > −margin**, with the margin configurable per task
  (default 3 points).
- **Judge:** only where gold doesn't exist, i.e. free-form or real playground traffic.
  - `distill/label_cli.py` shows ~200 examples blind (model identity hidden, order randomized)
    for you to label.
  - Cohen's kappa is then computed between you and the judge.
  - **If kappa < 0.6, the judge is not used in the gate**, and the result says so.
  - On the gold tasks I also report judge-vs-gold agreement as an extra calibration check.
- **Tests:** bootstrap CI on synthetic data with known coverage (≈95% across seeds); the gate
  decision at the margin boundary; kappa against hand-computed examples; the label CLI writes
  idempotently.

### M10: Rollout
**Matching on the hot path.** When `distill_rollout_enabled`:
- The request's alias must be a task's `source_alias`, **and** its system fingerprint must match.
  That's one dict lookup in memory; no embedding on the hot path.
- If the task is in `canary`/`full`, a random draw against the cached weight picks the arm.
- **Allowed models:** a diverted request is served by `distilled/<task>` *on behalf of* the
  alias the key was allowed to use. So `allowed_models` is checked against the **requested**
  alias, and the distilled substitute is permitted. Keys can opt out with
  `distill_serving = false`.
- **Canary v1 is non-streaming only.** Streams always go to the strong model. That's a stated
  limitation: validating a stream before sending it would mean buffering it.

**Shadow.** After the strong response is sent, the gateway calls the distilled model in a
background task with bounded concurrency. Results go to `distill.shadow.v1`. Shadow never affects
the response or its latency, and a shadow failure is only counted.

**Online validators and safety net.** Distilled outputs are validated before returning
(non-stream). An invalid output is **re-served by the strong model** in the same request: it
costs latency but is never a broken answer, and it's counted as a failure.

**Auto rollback.**
- Uses a 10-minute sliding window of distilled failures.
- Rolls back when `n ≥ min_samples` (default 100) **and** the Wilson 95% lower bound of the
  failure rate exceeds the threshold (default 5%).
- Atomic in Lua, so all replicas see it at once.
- Writes an audit row.

**Promotion.** shadow (agreement ≥ X over ≥ N samples) → 5% → 25% → 100%. Each step requires its
own minimum sample size with no rollback trigger. Every transition writes to `distill_audit`, and
the dashboard shows it.

**Tests:**
- Matcher hit and miss.
- The weight is honoured: 10k simulated draws stay within the binomial CI.
- `allowed_models` semantics.
- Shadow never delays the response or changes its status.
- Validator failure → strong re-serve.
- **Integration (real Redis, 3 app instances):** concurrent failures trigger exactly one rollback
  and one audit row, and the weight drops to 0 on all replicas within the cache TTL.
- k6: overhead with rollout enabled but no matching task (the common case) ≈ the baseline.

### M11: Automation
`uv run python -m distill.pipeline run --task <t>` (or `--all`) is a **resumable state machine**
stored in Postgres. Each step is idempotent:
1. discover
2. build
3. **wait for the trained artifact** (prints the Kaggle command; `--resume` continues once the
   GGUF is registered)
4. eval gate
5. shadow (for N samples)
6. canary 5 → 25 → 100
7. promote, or roll back

For nightly runs there's a `distill-worker` compose service with `--interval`, under the
`distill` profile.

**Tests:** state-machine transitions with fakes; a crash mid-step resumes without repeating work;
a failed gate stops before shadow.

### M12: Drift demo and results
**Drift:**
1. The replayer switches one task's input format mid-run (e.g. Banking77 queries embedded in a
   new email template), keeping the same system prompt.
2. So the fingerprint still matches, the distilled model keeps receiving traffic, and its outputs
   degrade.
3. The validator failure rate rises, and auto rollback triggers.

`distill/drift.py` writes a timeline (failure rate, weight, audit events) and a chart.

**README section:**
- an architecture diagram;
- a results table: % of traffic offloaded, cost per 1k requests before/after, quality delta with
  CI, latency;
- limitations;
- exact commands to reproduce.

Every number comes from a committed script output. Anything from simulated traffic is labelled
**simulated** in the table itself.

**Cost per 1k requests:** measured token counts × `models.yaml` list prices. This is a
*list-price equivalent*, labelled as such. The project's actual spend is $0, because everything
runs on free tiers. The distilled model's row shows $0 API cost, with a stated caveat:
self-hosted compute is **not priced**, because there's no paid host to measure. Its measured
throughput and memory are reported instead.

---

## 5. Hard rules: how each is enforced

| Rule | Enforcement |
|---|---|
| 1. Hot path unchanged, flags default off | Every new branch in `chat.py` is gated on a setting that defaults to False. New services sit behind the compose `distill` profile. The full existing suite runs in each milestone. A test asserts default settings create no producer, matcher or Kafka connection. |
| 2. ~0 capture latency | `put_nowait` only; all scrubbing and serialization happen in the background task. k6 before/after table with p50/p99 overhead from `Server-Timing`. |
| 3. Honesty | Every README number has a `results/*.json` source and a script. Failures (e.g. a model that fails the gate) get reported like M3's. |
| 4. Opt-in, PII scrubbing | `api_keys.distill_opt_in` (default false); content is never captured otherwise. Scrubbing happens before the event leaves the process, with unit tests. |
| 5. Simulated labelled | `simulated` column/field in the event, the Postgres row, Parquet, the dataset row, the dashboard badge, and every results table. |
| 6. One milestone at a time | I stop after each one. |

---

## 6. Risks

| # | Risk | Mitigation / what I'll report |
|---|---|---|
| R0 | **$0 budget**: no paid API, no VPS, no paid GPU | Free tiers only: Groq and Gemini free API keys, the mock LLM for noise traffic, free Kaggle/Colab GPUs. The OpenRouter balance is **not** used. A builder that hits a hard free-tier quota stops and resumes later instead of switching to a paid model. |
| R1 | **Teacher labelling on free tiers.** Rate limits cap dataset size, and I haven't checked today's Groq and Gemini limits. | Disk cache so every call is paid once; resumable builder; dataset sizes chosen *after* measuring the actual limit. I'll report the real n, even if smaller than hoped. |
| R2 | **0.5B/1.5B may fail the gate**, especially text-to-SQL | That's a valid result. Report it, and roll out only the tasks that pass. |
| R3 | **CPU llama.cpp latency may lose to Groq** | Report it. The claimed win is then cost and independence, not speed. |
| R4 | **Hot-path regressions**: capture, matching, rollout | Flags off by default; no per-request Redis reads; k6 before/after. |
| R5 | **8 GB Docker VM**: Kafka, consumer and llama.cpp | Kafka heap at 512 MB, memory limits, and a `distill` profile that's off by default. Training runs on Kaggle, not locally. |
| R6 | **Discovery is "easy"**, because we planted the tasks | Add noise traffic, and state plainly that discovery was validated on planted tasks. |
| R7 | **Teacher errors become labels.** Distillation copies the teacher's mistakes. | Gold-based eval measures both teacher and student against truth; the agreement filter drops uncertain labels. |
| R8 | **Train/test leakage** from near-duplicate prompts | Dedupe before splitting, plus a leakage unit test. |
| R9 | **Judge unreliability** | Kappa against your labels; below 0.6 the judge stays out of the gate. |
| R10 | **Single-broker Kafka isn't durable** | Capture is best-effort by design; ADR 0015 says so. Nothing on the money path depends on it. |
| R11 | **Self-hosted compute isn't priced** ($0 budget, no paid host) | The README says so explicitly next to the cost table; only measured throughput and memory are reported. |
| R12 | **Dataset licenses** | Verify before download; swap if needed. |
| R13 | **Scope**: 8 milestones | Each one is independently useful and shippable; stopping after any of them leaves a coherent repo. |
| R14 | **PII regexes are basic**: false negatives on unusual formats | Documented as "basic scrubbing", with tests listing what is and isn't caught. Data here is public and simulated. |

---

## 7. Decisions (made; constraint: $0 spend)

1. **Dependencies (all free, open source):**
   - `aiokafka`, as a `capture` extra;
   - `pyarrow`, in the `distill` group;
   - containers: `apache/kafka` (KRaft) and `ghcr.io/ggml-org/llama.cpp:server`;
   - training-only packages, pinned in `train/requirements.txt` and run on Kaggle: `unsloth`,
     `peft`, `transformers`, `trl`, `datasets`.
   - Not added: HF `datasets` locally, `sqlglot`, `hdbscan`.
2. **`CLAUDE.md`:** fine-tuning moves out of the non-goals; M5–M12 are added; the $0 rule is
   recorded.
3. **Tasks:** Banking77 (classification), WikiSQL (text-to-SQL), Few-NERD (JSON extraction).
   Licenses are verified before download.
4. **Teachers:** A = `groq-gpt-oss-120b`, B = `gemini-flash`, both on **free tiers**. No
   OpenRouter spend.
5. **Two per-key flags,** both default false:
   - `distill_opt_in` (capture content for training);
   - `distill_serving` (may be served by a distilled model).
   They're different consents, so they're kept separate.
6. **Canary v1 is non-streaming only.** Streams always go to the strong model.
7. **No hosting price is cited.** The distilled model's cost is reported as $0 API spend, with an
   explicit "compute not priced" caveat.
