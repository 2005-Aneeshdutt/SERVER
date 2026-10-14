"""Hugging Face datasets-server REST API (no `datasets` dependency), as eval/data.py uses."""

import random
from collections.abc import Callable, Iterator
from typing import Any

import httpx

ROWS_URL = "https://datasets-server.huggingface.co/rows"
PAGE = 100


def _page(
    client: httpx.Client, dataset: str, config: str, split: str, offset: int
) -> dict[str, Any]:
    params: dict[str, str | int] = {
        "dataset": dataset,
        "config": config,
        "split": split,
        "offset": offset,
        "length": PAGE,
    }
    response = client.get(ROWS_URL, params=params, timeout=60)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def features(dataset: str, config: str, split: str) -> list[dict[str, Any]]:
    with httpx.Client() as client:
        result: list[dict[str, Any]] = _page(client, dataset, config, split, 0)["features"]
        return result


FULL_SCAN_ROWS = 20_000


def sample_rows(
    dataset: str,
    config: str,
    split: str,
    n: int,
    seed: int,
    keep: Callable[[dict[str, Any]], bool] = lambda _: True,
) -> Iterator[dict[str, Any]]:
    """Yield up to n rows passing `keep`, in a seeded random order.

    Datasets up to FULL_SCAN_ROWS are read completely and shuffled as a whole. Bigger ones are
    sampled page by page at random offsets, which is only a fair sample when rows aren't
    sorted: Banking77's train split is sorted by label, and page sampling once produced
    "traffic" covering only 23 of its 77 intents (see distill/RESULTS.md).
    """
    rng = random.Random(seed)
    with httpx.Client() as client:
        total = int(_page(client, dataset, config, split, 0)["num_rows_total"])
        offsets = list(range(0, total, PAGE))
        if total <= FULL_SCAN_ROWS:
            rows = [
                r["row"] for o in offsets for r in _page(client, dataset, config, split, o)["rows"]
            ]
            rng.shuffle(rows)
            yield from [r for r in rows if keep(r)][:n]
            return
        rng.shuffle(offsets)
        produced = 0
        for offset in offsets:
            rows = [r["row"] for r in _page(client, dataset, config, split, offset)["rows"]]
            rng.shuffle(rows)
            for row in rows:
                if keep(row):
                    yield row
                    produced += 1
                    if produced >= n:
                        return
