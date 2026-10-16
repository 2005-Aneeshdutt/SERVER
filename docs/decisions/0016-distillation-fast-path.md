# 0016: Distilling one repeated task end to end, on a laptop, for $0

**Status:** accepted (M6–M10 fast path). Code: `distill/`, `train/`, `gateway/distill/`,
`gateway/providers/llamacpp.py`. Results: `distill/RESULTS.md`.

## Goal
Show the payoff of the closed loop on one real task before building it for all of them:
1. capture traffic;
2. find a narrow, repeated task sent to a strong model;
3. fine-tune a small model on the strong model's answers;
4. prove it's good enough;
5. route that task's traffic to it, with automatic rollback.

The task is **Banking77 intent classification**: 77 intents, one label per message. The source
is `groq-gpt-oss-120b`.

## Decisions, and what each traded away

### Discovery: exact system-prompt fingerprints, not embedding clusters
Apps that run a narrow task send the same system prompt every time. Grouping by its hash finds
the task at zero cost, and it's the same key the gateway matches at serving time, so discovery
and routing can't disagree. Groups are then scored by:
- volume;
- share of traffic sent to strong models (where the savings are);
- output stability (share of answers that fit a detected validator).

Open-ended chat has no system prompt and never forms a task.

**Traded away:** tasks whose apps vary the system prompt per request. Embedding clustering
(PLAN.md) would catch those; it's deferred.

### Labels: one teacher, with validators and ground truth for evaluation
Labels are the strong model's own captured answers. Rows are dropped when:
- the answer fails the task's validator (here, a label from the closed set);
- the prompt was altered by the PII scrubber;
- the input is a duplicate.

The split is a stable 75/25 by hash. Dataset gold labels are attached **for evaluation only**.

**Traded away:** agreement between two teachers (PLAN.md). The gold-based eval still measures
the teacher's own accuracy.

### Training: LoRA on CPU, in the serving format
- The fine-tuned model doesn't need Banking77's 77-label instructions. Training and serving
  both replace them with a short task tag (`gateway/distill/prompt.py`, shared code), so
  examples are ~40 tokens.
- LoRA (r=16) on the top half of Qwen2.5-0.5B-Instruct's layers. Backprop stops halfway down,
  which matters on CPU.
- Loss only on the answer tokens, fixed seeds.
- `train_config.json` records the dataset hash, hyperparameters and library versions.

The same script uses a GPU when one is present (Kaggle/Colab).

**Measured cost:** CPU training on this laptop is slow (about a minute per 16-example step while
other work shares the machine), so a run takes about an hour.

### Serving: llama.cpp, Q8_0 GGUF, behind the existing dispatcher
- Converted with llama.cpp's own converter (Docker image, nothing installed locally).
- Served by `llama-server` with 4 parallel slots and prompt-prefix cache reuse.
- `models.yaml` registers `distilled-banking77-intent-v1` (provider `llamacpp`, $0 per token)
  with **`groq-gpt-oss-120b` as its fallback**, so a dead llama.cpp server degrades to the
  strong model through the normal failover path.

### Gate: non-inferiority against ground truth, paired bootstrap
On the held-out split, both models are scored against the **gold** label:
- the strong model's actual captured answer;
- the distilled model's answer now.

A paired bootstrap of the accuracy difference (10,000 resamples, fixed seed) gives a 95% CI.
The gate passes iff the lower bound is above −3 points. `rollout_cli register` refuses a model
whose eval didn't pass.

**Traded away:** the LLM judge and human-label calibration (Cohen's kappa). Classification
has exact answers, so no judge is needed for this task. Those come back for free-form tasks.

### Rollout: shadow → canary → full, state in Redis, rollback in Lua
- **Matching:** the request's system-prompt fingerprint and requested alias identify the task.
  Task state is cached per replica for 2 s, so there's no Redis read per request.
- **Shadow:** the strong model answers. In the background, with bounded concurrency, the
  distilled model answers too, and validity and agreement are counted.
- **Canary / full:**
  - With probability `weight`, the distilled model answers.
  - Its answer is validated. **An invalid answer is re-served by the strong model in the same
    request**, so the client never sees a broken answer; it only waits longer.
  - Every outcome feeds a Lua script that keeps per-minute counters. It **rolls back
    atomically** when, over a 10-minute window, at least 50 distilled answers were seen and
    the 95% Wilson lower bound of the failure rate exceeds 5%.
  - The Wilson bound (instead of the raw rate) keeps a healthy model from being rolled back by
    noise. Atomicity means concurrent replicas can't both act; an integration test fires 60
    concurrent failures from 3 controllers and gets exactly one rollback and one audit row.
- **Consent and audit:** per-key `distill_serving` opt-in, separate from `distill_opt_in`
  (contributing training data). Every change goes to the `distill_audit` table and the
  dashboard: register, manual state change, automatic rollback.
- **Streams always use the strong model**, because a stream can't be validated before it's
  sent. A known limitation.

### Sampled audit: validity is not correctness (added after the drift finding)
Grammar-constrained decoding makes the distilled model always emit one of its known labels. That
removes invalid outputs, but it also blinds the validity check:
- On intents the model never saw, it is confidently wrong, and every answer is still "valid".
- The drift found in `distill/RESULTS.md` (90.5% of late traffic out of scope) would never have
  triggered the validity-based rollback.

The fix is a **sampled audit**:
- In canary and full, a share of served distilled answers (`DISTILL_AUDIT_RATE`, default 5%) is
  also sent to the strong model, in the background, with bounded concurrency.
- Disagreements go into a **second window** using the same atomic Lua script: rollback when at
  least 30 audits were seen and the Wilson lower bound of the disagreement rate exceeds 15%.
- The 15% sits above the ~7% disagreement measured at the eval gate, so a healthy model isn't
  rolled back by its normal level of disagreement.
- A failed strong call is never counted against the model.
- An integration test serves a model that gives only valid but wrong labels: the validity check
  never fires, the audit rolls it back exactly once, and an agreeing model is kept.

**The cost:** the audit rate is a direct trade between detection speed and strong-model spend. At
5%, detecting a 15%+ disagreement rate takes about 30 / 0.05 = 600 distilled requests.

## What "cheaper" means here, honestly
The distilled model has no per-token price, so list-price cost drops by the share of traffic it
serves. Its compute is **not free**, but it isn't priced either: there's no paid host to
measure under a $0 budget. The results report measured throughput and latency instead.
