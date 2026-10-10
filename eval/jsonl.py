"""Append-only JSONL caches: every API result is written as soon as it arrives, so an
interrupted run resumes where it stopped and nothing is paid for twice."""

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Appender:
    """Concurrency-safe appends from many tasks."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = asyncio.Lock()

    async def append(self, row: dict[str, Any]) -> None:
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
