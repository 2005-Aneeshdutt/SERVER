"""M6 dataset builder: turn one discovered task's captured traffic into a training set.

    docker compose --profile distill run --rm distill-tools python -m distill.build banking77-intent

Labels are the strong model's own answers (that's distillation). Filters, in order, each
counted in the manifest:
1. served OK by the task's source (strong) alias;
2. not altered by the PII scrubber (a scrubbed prompt no longer matches its original);
3. teacher output passes the task's validator;
4. deduplicated on the normalized user text.
Then a stable 75/25 train/test split by example-id hash. Dataset ground truth (`gold`) is
attached for evaluation only and never used as a training label.

Fast path vs PLAN.md: one teacher, not two. Agreement between two teachers is deferred; the
gold-based eval (M9) still measures the teacher's own accuracy.
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from distill.datasets import TASKS
from distill.discover import OUT, load_content
from gateway.core.config import Settings
from gateway.db.models import CaptureEventRow
from gateway.db.session import make_engine
from gateway.distill.prompt import task_tag
from gateway.distill.validators import normalize_label, validate

TEST_PERCENT = 25


async def redactions(settings: Settings) -> dict[str, int]:
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(select(CaptureEventRow.id, CaptureEventRow.pii_redactions))
            return {str(i): int(n) for i, n in rows}
    finally:
        await engine.dispose()


def _user_text(row: dict[str, Any]) -> str:
    messages = json.loads(row["messages_json"] or "[]")
    users = [m["content"] for m in messages if m["role"] == "user"]
    return str(users[-1]) if users else ""


def gold_by_text(task: str) -> dict[str, str]:
    """User text -> gold label, from the task file and any earlier versions of it
    (`<task>.<label>.jsonl`, e.g. the page-sampled set replayed before the sampling fix)."""
    gold: dict[str, str] = {}
    for path in [*sorted(TASKS.glob(f"{task}.*.jsonl")), TASKS / f"{task}.jsonl"]:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                gold[row["messages"][-1]["content"]] = row["gold"]
    return gold


def build(
    task: dict[str, Any],
    content: dict[str, dict[str, Any]],
    pii: dict[str, int],
    gold: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = task["validator"]
    counts: dict[str, int] = {}
    rows = sorted(
        (r for r in content.values() if r["system_fingerprint"] == task["system_fingerprint"]),
        key=lambda r: r["ts"],
    )
    counts["captured"] = len(rows)
    rows = [r for r in rows if r["status"] == "ok" and r["served_alias"] == task["source_alias"]]
    counts["served_by_source"] = len(rows)
    rows = [r for r in rows if pii.get(r["event_id"], 0) == 0]
    counts["not_scrubbed"] = len(rows)
    rows = [r for r in rows if validate(spec, r["response_text"])]
    counts["validator_passed"] = len(rows)
    examples, seen = [], set()
    for r in rows:
        text = _user_text(r)
        key = " ".join(text.lower().split())
        if not text or key in seen:
            continue
        seen.add(key)
        example_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        target = r["response_text"].strip()
        if spec["type"] == "label_set":
            target = normalize_label(target)
        split = "test" if int(example_id, 16) % 100 < TEST_PERCENT else "train"
        examples.append(
            {
                "example_id": example_id,
                "source_event_id": r["event_id"],
                "simulated": r["simulated"],
                "system": task_tag(task["task"]),
                "input": text,
                "target": target,
                "teacher": r["served_alias"],
                "gold": gold.get(text),  # eval only
                "split": split,
            }
        )
    counts["deduplicated"] = len(examples)
    counts["train"] = sum(e["split"] == "train" for e in examples)
    counts["test"] = sum(e["split"] == "test" for e in examples)
    lines = sorted(json.dumps(e, sort_keys=True) for e in examples)
    data_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    manifest = {
        "task": task["task"],
        "system_fingerprint": task["system_fingerprint"],
        "source_alias": task["source_alias"],
        "validator": spec,
        "teachers": [task["source_alias"]],
        "test_percent": TEST_PERCENT,
        "counts": counts,
        "data_hash": data_hash,
        "simulated": all(e["simulated"] for e in examples),
    }
    return examples, manifest


def write(examples: list[dict[str, Any]], manifest: dict[str, Any]) -> Path:
    out: Path = OUT / str(manifest["task"]) / str(manifest["data_hash"])[:12]
    out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "test"):
        with (out / f"{split}.jsonl").open("w", encoding="utf-8") as f:
            for e in examples:
                if e["split"] == split:
                    f.write(json.dumps(e) + "\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (OUT / manifest["task"] / "LATEST").write_text(out.name, encoding="utf-8")
    return out


def extend(task: dict[str, Any], examples: list[dict[str, Any]]) -> Path:
    """Add every example that isn't in the LATEST build to it as `test_extra.jsonl`: traffic
    captured after the model was trained, so no model has seen it. The build's train/test files
    and its data hash are untouched (the trained model stays tied to them)."""
    name = str(task["task"])
    build_dir: Path = OUT / name / (OUT / name / "LATEST").read_text(encoding="utf-8").strip()
    known = {
        json.loads(line)["example_id"]
        for split in ("train", "test")
        for line in (build_dir / f"{split}.jsonl").read_text("utf-8").splitlines()
    }
    extra = [{**e, "split": "test_extra"} for e in examples if e["example_id"] not in known]
    with (build_dir / "test_extra.jsonl").open("w", encoding="utf-8") as f:
        for e in extra:
            f.write(json.dumps(e) + "\n")
    manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"]["test_extra"] = len(extra)
    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return build_dir


def main() -> None:
    name = sys.argv[1]
    tasks = json.loads((OUT / "tasks.json").read_text(encoding="utf-8"))
    task = next(t for t in tasks if t["task"] == name)
    examples, manifest = build(
        task, load_content(), asyncio.run(redactions(Settings())), gold_by_text(name)
    )
    if "--extend" in sys.argv:
        out = extend(task, examples)
        print(f"test_extra: {len(examples) - manifest['counts']['deduplicated']} -> {out}")
        return
    out = write(examples, manifest)
    print(json.dumps(manifest["counts"]), f"-> {out}")


if __name__ == "__main__":
    main()
