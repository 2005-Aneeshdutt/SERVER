"""Paid step: download prompts, generate both models' answers, judge them. Fully cached.

uv run --group eval python -m eval.label             # resumes from eval/data/*.jsonl
uv run --group eval python -m eval.label --limit 20  # pilot on the first 20 prompts
uv run --group eval python -m eval.label --concurrency 32
"""

import asyncio
import sys
from decimal import Decimal

import httpx

from eval import jsonl
from eval.config import (
    CONSISTENCY_SAMPLE,
    GENERATIONS_FILE,
    JUDGMENTS_FILE,
    MAX_ANSWER_TOKENS,
    model_pair,
)
from eval.data import load_prompts
from eval.generate import generate
from eval.judge import judge
from gateway.core.config import Settings, secret_or_none
from gateway.providers.openrouter import OpenRouterProvider


def spent() -> Decimal:
    rows = jsonl.read(GENERATIONS_FILE) + jsonl.read(JUDGMENTS_FILE)
    return sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))


async def main() -> None:
    settings = Settings()
    key = secret_or_none(settings.openrouter_api_key)
    if key is None:
        sys.exit("OPENROUTER_API_KEY is required for labelling")
    prompts = load_prompts()
    if "--limit" in sys.argv:  # pilot runs: only the first N prompts
        prompts = prompts[: int(sys.argv[sys.argv.index("--limit") + 1])]
    concurrency = 8
    if "--concurrency" in sys.argv:
        concurrency = int(sys.argv[sys.argv.index("--concurrency") + 1])
    print(f"{len(prompts)} prompts; spent so far ${spent():.4f}", flush=True)
    cheap_spec, strong_spec = model_pair()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
        provider = OpenRouterProvider(client, settings.openrouter_base_url, key)
        cheap, strong = await asyncio.gather(
            generate(
                prompts, cheap_spec, provider, GENERATIONS_FILE, MAX_ANSWER_TOKENS, concurrency
            ),
            generate(
                prompts, strong_spec, provider, GENERATIONS_FILE, MAX_ANSWER_TOKENS, concurrency
            ),
        )
        swapped_ids = {p["id"] for p in sorted(prompts, key=lambda p: p["id"])[:CONSISTENCY_SAMPLE]}
        judgments = await judge(
            prompts, cheap, strong, provider, JUDGMENTS_FILE, swapped_ids, concurrency
        )
    print(
        f"done: {len(cheap)} cheap, {len(strong)} strong answers, {len(judgments)} judgments; "
        f"total spent ${spent():.4f}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
