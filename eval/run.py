"""Reproduce the routing evaluation from the committed labelled data (no API calls):

    uv run --extra router --group eval python -m eval.run

Embeds prompts with the gateway's embedder, trains the classifier, scores it against the
baselines on a held-out split, and writes eval/RESULTS.md, eval/cost_quality.svg and the
router artifact the gateway serves (gateway/routing/router_model.json).
"""

import json
import random
import sys
from collections.abc import Sequence
from typing import Any

from eval import jsonl
from eval.chart import Marker, Series, render
from eval.config import (
    CHART_FILE,
    CHEAP_ALIAS,
    JUDGE,
    JUDGMENTS_FILE,
    RESULTS_FILE,
    ROUTER_ARTIFACT,
    SEED,
    STRONG_ALIAS,
)
from eval.dataset import Example, load_examples, swap_consistency
from eval.embed import embed_examples
from eval.metrics import Point, apgr, bootstrap_apgr_diff, cpt, curve
from eval.train import (
    QUALITY_TARGETS,
    Trained,
    heuristic_score,
    predict,
    selection_study,
    stratified_split,
    train_classifier,
)
from gateway.routing.embedder import MAX_TOKENS, MODEL_NAME

BLUE, ORANGE, GREY = "#2563eb", "#ea580c", "#9ca3af"


def costs(examples: Sequence[Example]) -> tuple[list[float], list[float]]:
    return [e.cheap_cost for e in examples], [e.strong_cost for e in examples]


def evaluate(name: str, scores: Sequence[float], test: Sequence[Example]) -> dict[str, Any]:
    labels = [e.label for e in test]
    cheap, strong = costs(test)
    points = curve(scores, labels, cheap, strong)
    q_cheap = sum(labels) / len(labels)
    return {
        "name": name,
        "points": points,
        "apgr": apgr(points, q_cheap),
        "cpt50": cpt(points, q_cheap, 0.5),
        "cpt80": cpt(points, q_cheap, 0.8),
    }


def at_threshold(scores: Sequence[float], test: Sequence[Example], t: float) -> Point:
    cheap_route = [s >= t for s in scores]
    n = len(test)
    quality = sum((e.label if c else 1) for e, c in zip(test, cheap_route, strict=True)) / n
    cost = sum(
        (e.cheap_cost if c else e.strong_cost) for e, c in zip(test, cheap_route, strict=True)
    )
    return Point(1 - sum(cheap_route) / n, quality, cost, t)


def write_artifact(trained: Trained, n_train: int) -> None:
    artifact = {
        "cheap_alias": CHEAP_ALIAS,
        "strong_alias": STRONG_ALIAS,
        "embedding": {"model": MODEL_NAME, "max_tokens": MAX_TOKENS, "quantization": "int8"},
        "weights": [round(w, 6) for w in trained.weights],
        "bias": round(trained.bias, 6),
        "thresholds": {k: round(v, 6) for k, v in trained.thresholds.items()},
        "training": {
            "examples": n_train,
            "C": trained.c,
            "cv_log_loss": round(trained.cv_log_loss, 4),
        },
    }
    ROUTER_ARTIFACT.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    examples = load_examples()
    if len(examples) < 200:
        sys.exit("not enough labelled data: run `python -m eval.label` first")
    split = stratified_split(examples)
    vectors = embed_examples(examples)
    trained = train_classifier(split.train, vectors)
    study = selection_study(split.train, vectors)
    test = split.test
    clf_scores = [predict(trained, vectors[e.id]) for e in test]
    heur_scores = [heuristic_score(e) for e in test]
    rng = random.Random(SEED)
    rand_scores = [rng.random() for _ in test]
    results = [
        evaluate("Classifier (bge-small + logistic regression)", clf_scores, test),
        evaluate("Heuristic (length, code, math)", heur_scores, test),
        evaluate("Random", rand_scores, test),
    ]
    labels, (cheap, strong) = [e.label for e in test], costs(test)
    diff = bootstrap_apgr_diff(clf_scores, heur_scores, labels, cheap, strong)
    settings = {name: at_threshold(clf_scores, test, t) for name, t in trained.thresholds.items()}
    write_artifact(trained, len(split.train))
    q_cheap = sum(labels) / len(labels)
    CHART_FILE.write_text(
        render(
            "Routing Llama 3.1 8B vs Llama 3.3 70B",
            f"Held-out test set, {len(test)} Chatbot Arena prompts; "
            f"quality judged pairwise by {JUDGE.upstream_model}",
            [
                Series("Classifier", results[0]["points"], BLUE),
                Series("Heuristic", results[1]["points"], ORANGE, "6 4"),
                Series("Random", results[2]["points"], GREY, "2 4"),
            ],
            [Marker(name, p.strong_share, p.quality, BLUE) for name, p in settings.items()],
            q_cheap,
        ),
        encoding="utf-8",
    )
    RESULTS_FILE.write_text(
        report(examples, split.train, test, results, diff, settings, trained, study),
        encoding="utf-8",
    )
    print(
        f"APGR classifier {results[0]['apgr']:.3f} vs heuristic {results[1]['apgr']:.3f} "
        f"(diff {diff[0]:+.3f}, 95% CI {diff[1]:+.3f}..{diff[2]:+.3f}); wrote {RESULTS_FILE}"
    )


def report(
    examples: Sequence[Example],
    train: Sequence[Example],
    test: Sequence[Example],
    results: Sequence[dict[str, Any]],
    diff: tuple[float, float, float],
    settings: dict[str, Point],
    trained: Trained,
    study: Sequence[tuple[str, float, float | None]],
) -> str:
    agree, pairs = swap_consistency()
    n_test = len(test)
    q_cheap = sum(e.label for e in test) / n_test
    strong_cost = sum(e.strong_cost for e in test)
    cheap_cost = sum(e.cheap_cost for e in test)
    labelling_cost = sum(e.cheap_cost + e.strong_cost for e in examples)
    judge_cost = sum(float(j["cost_usd"]) for j in jsonl.read(JUDGMENTS_FILE))
    lines = [
        "# Routing evaluation",
        "",
        "_Generated by `uv run --extra router --group eval python -m eval.run` from the committed "
        "labelled data in `eval/data/`._",
        "",
        "![Cost vs quality](cost_quality.svg)",
        "",
        "## Setup",
        f"- **Prompts:** {len(examples)} single-turn English prompts sampled from "
        "[Chatbot Arena](https://huggingface.co/datasets/lmarena-ai/arena-human-preference-55k) "
        "(Apache-2.0).",
        f"- **Cheap:** `{CHEAP_ALIAS}` (Llama 3.1 8B). "
        f"**Strong:** `{STRONG_ALIAS}` (Llama 3.3 70B). "
        "Both via OpenRouter, temperature 0, max 512 tokens.",
        f"- **Labels:** {JUDGE.upstream_model} judges the two answers pairwise (order "
        "randomized per prompt). Label = 1 if the cheap answer wins or ties. "
        f"Base rate: the cheap answer is good enough on **{q_cheap:.1%}** of test prompts.",
        f"- **Judge consistency:** {agree}/{pairs} ({agree / max(pairs, 1):.0%}) of prompts got "
        "the same verdict with the answer order swapped; disagreements count as ties.",
        f"- **Split:** {len(train)} train / {n_test} test, stratified, seed {SEED}. Thresholds "
        "for the quality settings come from out-of-fold predictions on train only.",
        f"- **Classifier:** logistic regression (C={trained.c}) on int8 bge-small embeddings of "
        f"the first {MAX_TOKENS} tokens: the exact embedder the gateway serves.",
        "",
        "## Results (held-out test set)",
        "",
        "| Router | APGR ↑ | Strong calls for 50% of the gap (CPT50) ↓ | ... for 80% (CPT80) ↓ |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['name']} | **{r['apgr']:.3f}** | {r['cpt50']:.0%} | {r['cpt80']:.0%} |")
    verdict = "beats" if diff[1] > 0 else "does **not** significantly beat"
    lines += [
        "",
        f"Classifier minus heuristic APGR: **{diff[0]:+.3f}** (95% bootstrap CI {diff[1]:+.3f} to "
        f"{diff[2]:+.3f}, 1,000 resamples): the classifier {verdict} the heuristic baseline.",
        "",
        "APGR = average share of the cheap→strong quality gap recovered across all call budgets "
        "(random routing = 0.5). CPT = share of requests sent to the strong model needed to "
        "recover that much of the gap (lower is better). Metric definitions follow RouteLLM "
        "(Ong et al., LMSYS 2024).",
        "",
        "## What the per-key quality settings do (test set)",
        "",
        "| Setting | Target gap recovered | Strong calls | Quality | Cost vs always-strong |",
        "|---|---|---|---|---|",
    ]
    for name, p in settings.items():
        lines.append(
            f"| `{name}` | {QUALITY_TARGETS[name]:.0%} | {p.strong_share:.0%} | {p.quality:.1%} "
            f"| {p.cost / strong_cost:.0%} |"
        )
    lines += [
        f"| always cheap | — | 0% | {q_cheap:.1%} | {cheap_cost / strong_cost:.0%} |",
        "| always strong | — | 100% | 100% | 100% |",
        "",
        "## Model selection (5-fold CV on the training split only)",
        "",
        "Logistic regression was fixed in the plan. After the first test result, this study "
        "checked, using training data only, whether another model would have been better. "
        "None is by more than noise, so the simplest servable model stays.",
        "",
        "| Candidate | Out-of-fold APGR | Log loss |",
        "|---|---|---|",
        *[
            f"| {name} | {score:.3f} | {'—' if loss is None else f'{loss:.4f}'} |"
            for name, score, loss in study
        ],
        "",
        "## Interpretation",
        "",
        "- **The prompt carries little signal about whether the 8B answer will match the 70B "
        "answer.** Every router recovers only a few points more of the quality gap than random "
        "routing, and the classifier's log loss is barely below the base rate's. The two models "
        "are the same family trained on similar data, so they tend to succeed and fail on the "
        "same prompts. The differences are idiosyncratic per answer, not predictable from the "
        "prompt.",
        f"- **Label noise caps what any router can learn:** {100 - agree * 100 // max(pairs, 1)}% "
        "of verdicts flip when the answer order is swapped.",
        "- This matches RouteLLM's finding that routers trained only on Chatbot Arena "
        "preferences perform close to random, and need augmented or in-domain labels to help.",
        "- **What would move the result** (each costs a few dollars of labelling; the pipeline "
        "and caches support all three):",
        "  1. a model pair with a larger quality gap (e.g. 8B vs a frontier model);",
        "  2. more labels, plus in-domain data;",
        "  3. graded (1-10) judge scores instead of pairwise ties.",
        "",
        f"Labelling cost (per-call `cost` reported by OpenRouter): generations "
        f"${labelling_cost:.2f}, judge ${judge_cost:.2f}.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
