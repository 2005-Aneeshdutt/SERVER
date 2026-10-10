# 0012: Smart routing (`model: "auto"`) with rules and a prompt classifier

**Status:** accepted (M3). Code: `gateway/routing/smart.py`, `gateway/routing/embedder.py`,
`eval/`. Results: [`eval/RESULTS.md`](../../eval/RESULTS.md).

## Goal
Most prompts don't need the big model. Send each request to the cheap model when its answer is
likely to be as good as the strong model's, and keep quality high. The decision has to be
cheap: **no LLM call on the hot path**.

## Decision
1. **Hard rules first:**
   - required features (tools, images, JSON mode)
   - context length
   - provider circuit state
   - remaining monthly budget

   They're cheap to evaluate, and they're never wrong in a way that breaks the request. A rule
   that decides skips the embedding entirely.
2. **Then a classifier:** P(cheap answer ≥ strong answer) = logistic regression over the bge-small
   embedding of the last user message. Route to cheap if P ≥ the key's threshold
   (`routing_quality`: `low` / `balanced` / `high`).
3. **The model ships as plain JSON** (weights, bias, thresholds), not a pickle. Loading it can't
   execute code, and inference is a 384-term dot product with no sklearn at runtime.

## How the labels were made (`eval/label.py`)
- 2,000 single-turn English prompts from Chatbot Arena (Apache-2.0). Answers come from Llama 3.1 8B
  (cheap) and Llama 3.3 70B (strong): the same family at two sizes, so the label isolates
  "does this prompt need the bigger model?". Labels from both families mixed would mostly learn
  style differences.
- Gemini 2.5 Flash, a *different* family, judges pairwise. Label = 1 when the cheap answer wins
  or ties.
- **The judge prompt was fixed after a pilot.** A one-token verdict was position-biased: it chose
  "A" 26 times vs "B" 12, and swap consistency was 50%, a coin flip. Asking for one or two
  sentences of comparison before the verdict raised consistency to 75% for +$0.00009 per
  judgment. A subset is judged in both orders, and disagreements count as ties (the MT-Bench
  convention).
- Every API result is appended to a JSONL cache as it arrives, so labelling is paid for once and
  the data is committed. `eval/run.py` reproduces every number and the chart without API calls.

## Why these choices
- **Logistic regression, not an MLP:** about 1,600 training examples and a 384-dim input. A
  linear model is the right capacity, is calibrated enough to threshold, and is trivial to
  serve. An MLP would add variance without evidence that it helps.
- **Thresholds come from out-of-fold predictions on train**, never from the test split, so the
  reported quality of each setting is an honest estimate.
- **Evaluation follows RouteLLM** (Ong et al., LMSYS 2024): the cost/quality curve, APGR and
  CPT, against always-cheap, always-strong, random and a length/code/math heuristic. A bootstrap
  CI on the APGR difference says whether "beats the heuristic" is real or noise.

## Latency: the spec target was < 10 ms; we measured more
bge-small (33M parameters) on this laptop's CPU, in a Linux container:

| | short prompt | long prompt (128 tokens) |
|---|---|---|
| fp32 | ~31 ms | ~72 ms |
| int8 dynamic quantization, 4 threads (served) | ~18 ms | ~48 ms |

int8 changes embeddings only slightly (cosine 0.98 to fp32), and **the classifier is trained on
the same int8 embeddings it sees in production**: one `BgeEmbedder` class is used by both
`eval/` and the gateway, so there's no train/serve skew. Input is capped at 128 tokens because
the routing signal is in the start of the prompt.

The target isn't met on this hardware. In context, 20–50 ms is 1–5% of a typical LLM call, and
it decides whether that call costs 10× more. On a server CPU, or with ONNX Runtime (not in this
project's dependency budget), it would drop several-fold. Embedding runs in a dedicated worker
thread, so it never blocks the event loop, and a cache hit or a hard rule skips it entirely.

## Consequences
- `model: "auto"` requires the `router` extra (CPU torch + sentence-transformers, about 600 MB in
  the image) and `gateway/routing/router_model.json`. Without them the gateway works normally and
  `auto` returns a clear 400.
- Responses carry `X-Gateway-Route`, `-Route-Reason`, `-Route-Probability` and `-Route-Ms`.
  Request logs store `route` and `route_probability` for the dashboard (M4).
- The classifier is specific to this model pair. Changing either model means relabelling, which
  costs about $1.30 for 2,000 prompts with the caches.
