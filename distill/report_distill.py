"""M6-M10 sections of distill/RESULTS.md, rendered only from committed outputs:
data/distill/tasks.json and dataset manifests (M6), distill/results/train_*.json (M7),
eval_*.json (M9), payoff_*.json and the distill_audit table (M10)."""

import json
from pathlib import Path
from typing import Any

from distill.discover import OUT

RESULTS = Path("distill/results")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def discovery() -> list[str]:
    tasks = _json(OUT / "tasks.json")
    if not tasks:
        return []
    lines = [
        "## M6: task discovery and dataset",
        "",
        "Traffic grouped by system-prompt fingerprint and scored "
        "(`python -m distill.discover`; see ADR 0016):",
        "",
        "| Task | Events | Strong-model share | Output stability | Validator | Score |",
        "|---|---|---|---|---|---|",
    ]
    for t in tasks:
        v = (t.get("validator") or {}).get("type", "none")
        if "strong_share" in t:
            lines.append(
                f"| {t['task']} | {t['events']} | {_pct(t['strong_share'])} "
                f"| {_pct(t['stability'])} | {v} | {t['score']} |"
            )
        else:
            lines.append(f"| {t['task']} | {t['events']} | - | - | - | {t['score']} |")
    for name in [t["task"] for t in tasks if (OUT / t["task"] / "LATEST").exists()]:
        build = OUT / name / (OUT / name / "LATEST").read_text(encoding="utf-8").strip()
        m = _json(build / "manifest.json")
        c = m["counts"]
        lines += [
            "",
            f"**Dataset `{name}`** (`{m['data_hash'][:12]}`, teacher `{m['source_alias']}`, "
            f"simulated: {m['simulated']}): {c['captured']} captured, then "
            f"{c['served_by_source']} served by the source model, {c['not_scrubbed']} unaltered "
            f"by the PII scrubber, {c['validator_passed']} passing the validator, "
            f"{c['deduplicated']} after dedup: **{c['train']} train / {c['test']} test**.",
        ]
        labels = m["validator"].get("labels")
        if name == "banking77-intent" and labels:
            lines += [
                "",
                f"**Known data bug:** the traffic covers only **{len(labels)} of Banking77's 77 "
                "intents**. Banking77's train split is sorted by label, and the replayer's loader "
                "sampled whole pages at random offsets, so 1,200 messages came from a few dozen "
                "pages. Everything below (training, eval, payoff) is therefore about this "
                f"{len(labels)}-intent slice, not a 77-intent classifier. The loader now shuffles "
                "small datasets globally (`distill/datasets/hf.py`, with a test); new traffic "
                "needs Groq's daily quota.",
            ]
    return lines


VERSION_NOTES = {
    "v1": "LoRA on the top 12 of 24 layers, 3 epochs",
    "v2": "v1's weights + grammar-constrained decoding (only valid labels can be generated)",
    "v3": "LoRA on all 24 layers, 4 epochs, + grammar-constrained decoding",
}


def _versions(task: str) -> list[str]:
    stems = (p.stem.rsplit("_", 1)[1] for p in RESULTS.glob(f"eval_{task}_v*.json"))
    return sorted((v for v in stems if v[1:].isdigit()), key=lambda v: int(v[1:]))


def extended_gate(task: str) -> list[str]:
    """The one pre-committed re-run of the gate on a larger held-out set (see the text)."""
    runs = sorted(RESULTS.glob(f"eval_{task}_v*-extended.json"))
    if not runs:
        return []
    lines = [
        "",
        "### The gate was underpowered: one pre-committed re-run on more held-out data",
        "",
        "v3 tied the strong model exactly on the original 185 examples, yet failed: with "
        "n = 185 the 95% CI is about ±3.8 points wide, so its lower bound can't clear -3 even "
        "for a perfect tie. That is a design flaw in the gate (no power calculation), not "
        "evidence about the model. The fix chosen *before* looking at any new result: replay "
        "every remaining unseen example from the same traffic, add them all as `test_extra` "
        "(captured after training, so no model has seen them), and run the gate **once** on "
        "the combined set with the **same 3-point margin**, accepting the outcome either way. "
        "No other re-runs, no margin change. The builder's filters still apply to the new "
        "examples, and most of the new traffic turned out to be out of scope (see Drift below), "
        "so far fewer examples were added than expected.",
        "",
        "| Version | n | Distilled | Strong | Difference | 95% CI | Gate |",
        "|---|---|---|---|---|---|---|",
    ]
    for path in runs:
        e = _json(path)
        lines.append(
            f"| {e['version']} | {e['n']} | {_pct(e['distilled_accuracy'])} "
            f"| {_pct(e['strong_accuracy'])} | {e['diff'] * 100:+.1f} pts "
            f"| [{e['ci95'][0] * 100:+.1f}, {e['ci95'][1] * 100:+.1f}] "
            f"| **{'PASS' if e['passed'] else 'FAIL'}** |"
        )
    return lines


def training(task: str) -> list[str]:
    rows = []
    for version in _versions(task):
        cfg = _json(RESULTS / f"train_{task}_{version}.json")
        if not cfg:
            continue
        last = cfg["loss_curve"][-1]["loss"] if cfg["loss_curve"] else None
        rows.append(
            f"| {version} | {VERSION_NOTES.get(version, '')} | {cfg['lora']['layers'][0]}-"
            f"{cfg['lora']['layers'][1]} | {cfg['epochs']} | {cfg['steps']} "
            f"| {cfg['wall_seconds'] / 60:.0f} min | {last} |"
        )
    if not rows:
        return []
    cfg = _json(RESULTS / f"train_{task}_{_versions(task)[0]}.json")
    return [
        "",
        "## M7: training",
        "",
        f"`{cfg['base_model']}` + LoRA r={cfg['lora']['r']}, batch {cfg['batch_size']}, "
        f"lr {cfg['learning_rate']}, seed {cfg['seed']}, {cfg['train_rows']} training rows, on "
        f"this laptop's **{cfg['device']}**. Merged, then converted to a Q8_0 GGUF "
        "(`scripts/to_gguf.sh`). Configs: `distill/results/train_<task>_<version>.json`.",
        "",
        "| Version | What changed | LoRA layers | Epochs | Steps | Wall time | Final loss |",
        "|---|---|---|---|---|---|---|",
        *rows,
    ]


def gate(task: str) -> list[str]:
    evals = [(v, _json(RESULTS / f"eval_{task}_{v}.json")) for v in _versions(task)]
    if not evals:
        return []
    first = evals[0][1]
    lines = [
        "",
        "## M9: eval gate (held-out test split, scored against dataset gold labels)",
        "",
        f"n = {first['n']} held-out examples. Strong model (`groq-gpt-oss-120b`, its captured "
        f"answers): **{_pct(first['strong_accuracy'])}**. Gate: paired bootstrap "
        f"({first['bootstrap']['resamples']:,} resamples, seed {first['bootstrap']['seed']}) of "
        "accuracy(distilled) - accuracy(strong); PASS iff the 95% CI lower bound is above "
        f"-{first['margin'] * 100:.0f} points. The margin was fixed before the first run and "
        "never changed.",
        "",
        "| Version | Distilled accuracy | Valid outputs | Agreement with strong | Difference "
        "| 95% CI | Gate | Latency p50 / p95 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for version, e in evals:
        lines.append(
            f"| {version} | {_pct(e['distilled_accuracy'])} | {_pct(e['distilled_valid'])} "
            f"| {_pct(e['agreement_with_strong'])} | {e['diff'] * 100:+.1f} pts "
            f"| [{e['ci95'][0] * 100:+.1f}, {e['ci95'][1] * 100:+.1f}] "
            f"| **{'PASS' if e['passed'] else 'FAIL'}** "
            f"| {e['distilled_latency_ms']['p50']:.0f} / "
            f"{e['distilled_latency_ms']['p95']:.0f} ms |"
        )
    widest = max((e["ci95"][1] - e["ci95"][0]) / 2 for _, e in evals)
    if widest >= first["margin"]:
        lines += [
            "",
            f"**The gate is underpowered at n = {first['n']}:** the 95% CI is up to "
            f"±{widest * 100:.1f} points wide, more than the {first['margin'] * 100:.0f}-point "
            "margin, so even a model that ties the strong model exactly (v3) can't pass. More "
            "held-out data is needed, not a looser margin.",
        ]
    if len(evals) > 1:
        lines += [
            "",
            f"The same test split was used for all {len(evals)} versions, so later versions "
            "had more looks at it (mild risk of adapting to the test set). Latency is llama.cpp "
            "on this laptop's CPU (4 threads).",
        ]
    return lines


def payoff(task: str) -> list[str]:
    p = _json(RESULTS / f"payoff_{task}.json")
    if not p:
        return []
    b, a = p["before"], p["after"]
    return [
        "",
        "## M10: rollout payoff (same held-out requests, before and after)",
        "",
        "| | Served by distilled | Accuracy vs gold | List-price cost / 1k requests "
        "| Latency p50 | p95 |",
        "|---|---|---|---|---|---|",
        *(
            f"| {r['label']} | {_pct(r['served_by_distilled'])} | {_pct(r['accuracy_vs_gold'])} "
            f"| ${r['cost_per_1k_usd']} | {r['latency_ms_p50']:.0f} ms "
            f"| {r['latency_ms_p95']:.0f} ms |"
            for r in (b, a)
        ),
        "",
        f"- {a['n']} requests; {p['fallbacks']} distilled answers failed validation and were "
        "re-served by the strong model in the same request.",
        "- Simulated traffic. Cost is list price; the distilled model's self-hosted compute is "
        "**not priced** (no paid host under the $0 budget). Before-latency is the strong model "
        "via Groq's API as captured; after-latency includes llama.cpp on this laptop's CPU.",
    ]


def audit(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return []
    return [
        "",
        "### Rollout audit trail (`distill_audit`)",
        "",
        "| Time (UTC) | Task | Action | State | Weight | Actor | Reason |",
        "|---|---|---|---|---|---|---|",
        *(
            f"| {e['ts'][:19]} | {e['task']} | {e['action']} | {e['state']} | {e['weight']:g} "
            f"| {e['actor']} | {e['reason']} |"
            for e in reversed(events)
        ),
    ]


def drift(task: str, content: dict[str, dict[str, Any]]) -> list[str]:
    """How much of the task's traffic, captured after the dataset was built, falls outside the
    label set the model was trained on (measured from the strong model's own answers)."""
    from gateway.distill.validators import validate

    latest = OUT / task / "LATEST"
    if not latest.exists():
        return []
    build = OUT / task / latest.read_text(encoding="utf-8").strip()
    manifest = _json(build / "manifest.json")
    known = {
        json.loads(line)["source_event_id"]
        for split in ("train", "test")
        for line in (build / f"{split}.jsonl").read_text("utf-8").splitlines()
    }
    rows = [
        r
        for r in content.values()
        if r["system_fingerprint"] == manifest["system_fingerprint"]
        and r["status"] == "ok"
        and r["served_alias"] == manifest["source_alias"]
    ]
    built_at = max(r["ts"] for r in rows if r["event_id"] in known)
    before = [r for r in rows if r["ts"] <= built_at]  # all traffic up to the dataset build
    after = [r for r in rows if r["ts"] > built_at]
    if not after:
        return []
    out_before = sum(not validate(manifest["validator"], r["response_text"]) for r in before)
    out_after = sum(not validate(manifest["validator"], r["response_text"]) for r in after)
    labels = len(manifest["validator"]["labels"])
    return [
        "",
        "### Drift: new traffic the distilled model can't handle",
        "",
        f"The {labels}-label set comes from the traffic the model was trained on. Of the "
        f"{len(before)} requests captured up to the dataset build, {out_before} "
        f"({_pct(out_before / len(before))}) had a strong-model answer outside it. Of the "
        f"**{len(after)} requests captured after the dataset was built, {out_after} "
        f"({_pct(out_after / len(after))})** did: the later traffic is mostly intents the model "
        "has never seen (a side effect of the page-sampling bug, but exactly what real drift "
        "looks like).",
        "",
        "**What this exposes in the design:** grammar-constrained decoding makes the distilled "
        "model always answer with one of its known labels, so the online *validity* check can "
        "never fire on out-of-scope inputs. The model would be confidently wrong and the "
        "rollback signal would stay silent. Validity is not correctness. Only comparison with "
        "the strong model catches it: shadow agreement, or a sampled share of canary/full "
        "traffic also sent to the strong model. That audit sample is the missing rollback "
        "input, and it is the next change to make.",
    ]


def render_distillation(
    events: list[dict[str, Any]], task: str, content: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    return [
        *discovery(),
        *training(task),
        *gate(task),
        *extended_gate(task),
        *(drift(task, content) if content else []),
        *payoff(task),
        *audit(events),
    ]
