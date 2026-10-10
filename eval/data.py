"""Download and filter prompts from the Chatbot Arena dataset (HF datasets-server API)."""

import hashlib
import json
import random
import re
from typing import Any

import httpx

from eval import jsonl
from eval.config import DATASET, MAX_CHARS, MIN_CHARS, N_PROMPTS, PROMPTS_FILE, SEED

ROWS_URL = "https://datasets-server.huggingface.co/rows"
PAGE = 100
_WS = re.compile(r"\s+")


def _single_turn_prompt(row: dict[str, Any]) -> str | None:
    """The dataset stores conversations as JSON lists of user turns; keep single-turn ones."""
    try:
        turns = json.loads(row["prompt"])
    except (ValueError, TypeError):
        return None
    if not isinstance(turns, list) or len(turns) != 1 or not isinstance(turns[0], str):
        return None
    return turns[0].strip()


def _keep(text: str) -> bool:
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        return False
    ascii_share = sum(c.isascii() for c in text) / len(text)
    return ascii_share >= 0.95  # rough English filter: the models and judge are English-first


def _fetch_page(client: httpx.Client, offset: int) -> list[dict[str, Any]]:
    params: dict[str, str | int] = {
        "dataset": DATASET,
        "config": "default",
        "split": "train",
        "offset": offset,
        "length": PAGE,
    }
    response = client.get(ROWS_URL, params=params, timeout=60)
    response.raise_for_status()
    return [r["row"] for r in response.json()["rows"]]


def download() -> list[dict[str, str]]:
    """Sample pages at random offsets (seeded) until N unique prompts pass the filters."""
    rng = random.Random(SEED)
    with httpx.Client() as client:
        total = int(
            client.get(
                ROWS_URL,
                params={
                    "dataset": DATASET,
                    "config": "default",
                    "split": "train",
                    "offset": 0,
                    "length": 1,
                },
                timeout=60,
            ).json()["num_rows_total"]
        )
        offsets = rng.sample(range(0, total - PAGE, PAGE), k=(total - PAGE) // PAGE)
        seen: set[str] = set()
        prompts: list[dict[str, str]] = []
        for offset in offsets:
            for row in _fetch_page(client, offset):
                text = _single_turn_prompt(row)
                if text is None or not _keep(text):
                    continue
                key = _WS.sub(" ", text.lower())
                if key in seen:
                    continue
                seen.add(key)
                prompts.append(
                    {"id": hashlib.sha256(key.encode()).hexdigest()[:16], "prompt": text}
                )
            if len(prompts) >= N_PROMPTS:
                break
    prompts = prompts[:N_PROMPTS]
    jsonl.write(PROMPTS_FILE, prompts)
    return prompts


def load_prompts() -> list[dict[str, str]]:
    cached = jsonl.read(PROMPTS_FILE)
    return [{"id": str(p["id"]), "prompt": str(p["prompt"])} for p in cached] or download()
