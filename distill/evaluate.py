"""M9 eval gate: is the distilled model non-inferior to the strong model on held-out data?

    uv run --group distill python -m distill.evaluate banking77-intent --version v1

For every example in the dataset's held-out `test` split (never trained on):
- the **strong** model's answer is the one it actually gave in captured traffic (the teacher
  output kept in the dataset row);
- the **distilled** model answers now, through llama.cpp, in its serving format;
- both are scored against the dataset's **gold** label (exact match after normalization), so
  no LLM judge is needed for this task.

Gate: paired bootstrap (10,000 resamples, fixed seed) of accuracy(distilled) - accuracy(strong).
PASS iff the lower end of the 95% CI is above -margin (default 3 points): the distilled model is
at most `margin` worse, with 95% confidence. Results: distill/results/eval_<task>_<version>.json.

Caveat: the dataset keeps only teacher outputs that passed the validator, so the strong model is
scored on its valid answers (~98% of its traffic). That flatters the strong model slightly, which
makes the gate stricter, not looser.
"""

import argparse
import json
import random
import statistics
import time
import urllib.request
from pathlib import Path
from typing import Any

from distill.discover import OUT
from gateway.distill.prompt import distilled_messages
from gateway.distill.validators import normalize_label, validate

RESULTS = Path("distill/results")


def load_split(task: str, split: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    build = OUT / task / (OUT / task / "LATEST").read_text(encoding="utf-8").strip()
    manifest = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (build / f"{split}.jsonl").read_text("utf-8").splitlines()]
    return rows, manifest


def ask(url: str, model: str, task: str, text: str) -> tuple[str, float]:
    body = {
        "model": model,
        "messages": distilled_messages(task, text),
        "temperature": 0,
        "max_tokens": 32,
        "cache_prompt": True,
    }
    request = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
    return str(data["choices"][0]["message"]["content"] or ""), time.perf_counter() - started


def paired_bootstrap(diffs: list[int], resamples: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def evaluate(
    rows: list[dict[str, Any]], answers: list[str], validator: dict[str, Any]
) -> dict[str, Any]:
    scored = [r for r in rows if r.get("gold")]
    gold = [normalize_label(r["gold"]) for r in scored]
    by_id = {r["example_id"]: a for r, a in zip(rows, answers, strict=True)}
    distilled = [normalize_label(by_id[r["example_id"]]) for r in scored]
    strong = [normalize_label(r["target"]) for r in scored]
    d_ok = [int(p == g) for p, g in zip(distilled, gold, strict=True)]
    s_ok = [int(p == g) for p, g in zip(strong, gold, strict=True)]
    return {
        "n": len(scored),
        "distilled_accuracy": sum(d_ok) / len(scored),
        "strong_accuracy": sum(s_ok) / len(scored),
        "distilled_valid": sum(validate(validator, a) for a in answers) / len(answers),
        "agreement_with_strong": sum(p == s for p, s in zip(distilled, strong, strict=True))
        / len(scored),
        "diffs": [d - s for d, s in zip(d_ok, s_ok, strict=True)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--url", default="http://localhost:8081/v1/chat/completions")
    parser.add_argument("--margin", type=float, default=0.03)
    parser.add_argument("--resamples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rows, manifest = load_split(args.task, "test")
    model = f"{args.task}-{args.version}"
    answers, latencies = [], []
    for i, row in enumerate(rows, 1):
        answer, seconds = ask(args.url, model, args.task, row["input"])
        answers.append(answer)
        latencies.append(seconds)
        if i % 50 == 0:
            print(f"  {i}/{len(rows)}", flush=True)
    result = evaluate(rows, answers, manifest["validator"])
    lo, hi = paired_bootstrap(result.pop("diffs"), args.resamples, args.seed)
    diff = result["distilled_accuracy"] - result["strong_accuracy"]
    record = {
        "task": args.task,
        "version": args.version,
        "dataset_hash": manifest["data_hash"],
        "simulated": manifest["simulated"],
        "metric": "accuracy vs dataset gold label (exact match)",
        **{k: round(v, 4) if isinstance(v, float) else v for k, v in result.items()},
        "diff": round(diff, 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "bootstrap": {"resamples": args.resamples, "seed": args.seed, "paired": True},
        "margin": args.margin,
        "passed": lo > -args.margin,
        "distilled_latency_ms": {
            "p50": round(statistics.median(latencies) * 1000, 1),
            "p95": round(sorted(latencies)[int(0.95 * len(latencies)) - 1] * 1000, 1),
        },
        "examples": [
            {
                "example_id": r["example_id"],
                "gold": r.get("gold"),
                "strong": r["target"],
                "distilled": a,
            }
            for r, a in zip(rows, answers, strict=True)
        ],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"eval_{args.task}_{args.version}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    summary = {k: v for k, v in record.items() if k != "examples"}
    print(json.dumps(summary, indent=2))
    print(f"GATE: {'PASS' if record['passed'] else 'FAIL'} -> {path}")


if __name__ == "__main__":
    main()
