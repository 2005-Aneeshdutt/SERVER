"""Public datasets that stand in for production traffic (all traffic built from them is
labelled simulated). Each loader writes `data/tasks/<task>.jsonl`:

    {"task", "example_id", "messages", "response_format", "gold", "meta"}

`messages` is exactly what the replayer sends. `gold` is the dataset's ground truth: it is
**never sent** and never used for training, only for evaluation (M6/M9).
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DATA = Path("data")
TASKS = DATA / "tasks"


@dataclass(frozen=True)
class TaskExample:
    task: str
    example_id: str
    messages: list[dict[str, str]]
    gold: str
    response_format: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def example_id(task: str, text: str) -> str:
    return hashlib.sha256(f"{task}\n{' '.join(text.lower().split())}".encode()).hexdigest()[:16]


def write(task: str, examples: list[TaskExample]) -> Path:
    TASKS.mkdir(parents=True, exist_ok=True)
    path = TASKS / f"{task}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in examples:
            f.write(json.dumps(asdict(e)) + "\n")
    return path


def read(task: str) -> list[TaskExample]:
    path = TASKS / f"{task}.jsonl"
    with path.open(encoding="utf-8") as f:
        return [TaskExample(**json.loads(line)) for line in f if line.strip()]
