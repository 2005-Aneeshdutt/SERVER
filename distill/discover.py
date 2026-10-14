"""M6 task discovery: find narrow, repeated tasks in captured traffic.

    docker compose --profile distill run --rm distill-tools python -m distill.discover

Fast path (see distill/RESULTS.md): traffic is grouped by **system-prompt fingerprint**, not by
embedding clusters. Apps that run a narrow task send the same system prompt every time, so an
exact fingerprint finds the task at no cost, and it's the same key the gateway matches on at
serving time. Requests without a system prompt (open-ended chat) never form a task.

Each group is scored:
- volume:      its share of all captured traffic
- strong share: how much of it went to a `strong`-tier model (that's where savings are)
- stability:   share of outputs that fit the detected validator (a consistent output shape)
- checkable:   whether a validator could be detected at all (label set, JSON keys, SQL)
score = volume * strong share * stability * checkable
"""

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from distill.datasets import TASKS, read
from distill.replay import TASK_NAMES
from gateway.capture.fingerprint import system_fingerprint
from gateway.core.config import Settings
from gateway.core.schemas import ChatMessage
from gateway.distill.validators import detect, validate
from gateway.routing.registry import ModelRegistry

OUT = Path("data/distill")
PARQUET = Path(os.environ.get("CAPTURE_PARQUET_DIR", "data/capture"))


def known_names() -> dict[str, str]:
    """fingerprint -> dataset task name, for readable reports (simulated traffic only)."""
    out = {}
    for task in TASK_NAMES:
        if (TASKS / f"{task}.jsonl").exists():
            first = read(task)[0]
            fp = system_fingerprint([ChatMessage.model_validate(m) for m in first.messages])
            if fp:
                out[fp] = task
    return out


def load_content(root: Path = PARQUET) -> dict[str, dict[str, Any]]:
    """event_id -> captured content row, deduplicated (at-least-once writes)."""
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.parquet")):
        for row in pq.read_table(path).to_pylist():
            rows.setdefault(row["event_id"], row)
    return rows


def discover(
    content: dict[str, dict[str, Any]], registry: ModelRegistry, names: dict[str, str]
) -> list[dict[str, Any]]:
    groups: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for row in content.values():
        if row["status"] == "ok":
            groups[row["system_fingerprint"]].append(row)
    total = sum(map(len, groups.values()))
    tiers = {s.alias: s.tier for s in registry.all()}
    candidates: list[dict[str, Any]] = []
    for fp, rows in groups.items():
        if fp is None:
            continue  # no system prompt: open-ended traffic, not a narrow task
        outputs = [r["response_text"] or "" for r in rows]
        spec = detect(outputs)
        stability = sum(validate(spec, o) for o in outputs) / len(outputs) if spec else 0.0
        strong = sum(tiers.get(r["served_alias"]) == "strong" for r in rows) / len(rows)
        aliases: dict[str, int] = defaultdict(int)
        for r in rows:
            aliases[r["served_alias"]] += 1
        volume = len(rows) / total
        candidates.append(
            {
                "task": names.get(fp, f"task-{fp[:8]}"),
                "system_fingerprint": fp,
                "events": len(rows),
                "volume": round(volume, 4),
                "strong_share": round(strong, 4),
                "stability": round(stability, 4),
                "validator": spec,
                "source_alias": max(aliases, key=lambda a: aliases[a]),
                "simulated": all(r["simulated"] for r in rows),
                "score": round(volume * strong * stability, 4),
            }
        )
    no_system = len(groups.get(None, []))
    candidates.sort(key=lambda c: -float(c["score"]))
    return [*candidates, {"task": "(no system prompt)", "events": no_system, "score": 0.0}]


def main() -> None:
    content = load_content()
    registry = ModelRegistry.from_yaml(Settings().models_file)
    tasks = discover(content, registry, known_names())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tasks.json").write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    for t in tasks:
        validator = (t.get("validator") or {}).get("type", "-")
        print(
            f"{t['task']:<24} events {t['events']:>5}  score {t['score']:<7} "
            f"strong {t.get('strong_share', '-')} stability {t.get('stability', '-')} "
            f"validator {validator}"
        )


if __name__ == "__main__":
    main()
