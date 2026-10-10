"""Pairwise LLM judge: is the cheap model's answer at least as good as the strong model's?

The judge sees the two answers as "A" and "B" in an order derived from the prompt id (so it's
random across prompts but reproducible). A subset is judged again with the order swapped to
measure position bias. Label = 1 if the cheap answer wins or ties.
"""

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from eval import jsonl
from eval.config import JUDGE
from eval.generate import billed_cost, complete_with_retries
from gateway.core.schemas import ChatRequest
from gateway.providers.base import Provider

# v1 asked for a one-token verdict and showed strong position bias in a pilot (it picked "A"
# 26 times vs "B" 12 times; swap consistency 50%, i.e. a coin flip). Asking for one or two
# sentences of comparison first brought swap consistency to 75%, typical for LLM judges.
SYSTEM = (
    "You are an impartial judge comparing two AI assistant answers to the same user request. "
    "Evaluate correctness first, then helpfulness, completeness and clarity. Answers may be cut "
    "off at a length limit: judge the content that is shown and do not penalize the cut-off "
    "itself. Ignore answer length and the order in which the answers appear. First write one or "
    "two short sentences comparing the answers, then on the last line write exactly "
    "'Verdict: A', 'Verdict: B' or 'Verdict: TIE'."
)
TEMPLATE = "[User request]\n{prompt}\n\n[Answer A]\n{a}\n\n[Answer B]\n{b}"
_VERDICT = re.compile(r"Verdict:\s*(A|B|TIE)\b", re.IGNORECASE)


def cheap_is_a(prompt_id: str, swapped: bool = False) -> bool:
    first = hashlib.sha256(prompt_id.encode()).digest()[0] % 2 == 0
    return first != swapped


def parse_verdict(text: str) -> str | None:
    matches = _VERDICT.findall(text)
    return str(matches[-1]).upper() if matches else None


def outcome(verdict: str, cheap_first: bool) -> str:
    """Map A/B/TIE to cheap/strong/tie."""
    if verdict == "TIE":
        return "tie"
    cheap_letter = "A" if cheap_first else "B"
    return "cheap" if verdict == cheap_letter else "strong"


async def judge(
    prompts: list[dict[str, str]],
    cheap: dict[str, dict[str, Any]],
    strong: dict[str, dict[str, Any]],
    provider: Provider,
    cache: Path,
    swapped_ids: set[str],
    concurrency: int = 8,
) -> list[dict[str, Any]]:
    done = {(r["id"], r["swapped"]): r for r in jsonl.read(cache)}
    jobs = [(p, False) for p in prompts] + [(p, True) for p in prompts if p["id"] in swapped_ids]
    jobs = [
        (p, s)
        for p, s in jobs
        if p["id"] in cheap and p["id"] in strong and (p["id"], s) not in done
    ]
    appender = jsonl.Appender(cache)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(item: dict[str, str], swapped: bool) -> None:
        cheap_first = cheap_is_a(item["id"], swapped)
        c, s = cheap[item["id"]]["answer"], strong[item["id"]]["answer"]
        a, b = (c, s) if cheap_first else (s, c)
        request = ChatRequest.model_validate(
            {
                "model": JUDGE.alias,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": TEMPLATE.format(prompt=item["prompt"], a=a, b=b)},
                ],
                "temperature": 0,
                "max_tokens": 150,
                "reasoning": {"enabled": False},  # OpenRouter: no paid thinking tokens
            }
        )
        async with semaphore:
            response = await complete_with_retries(provider, request, JUDGE)
        if response is None or not response.choices:
            return
        text = response.choices[0].message.content or ""
        verdict = parse_verdict(text)
        if verdict is None:
            return
        row = {
            "id": item["id"],
            "swapped": swapped,
            "cheap_first": cheap_first,
            "verdict": verdict,
            "rationale": text.strip(),
            "winner": outcome(verdict, cheap_first),
            "cost_usd": billed_cost(response, JUDGE),
        }
        done[(item["id"], swapped)] = row
        await appender.append(row)

    if jobs:
        print(f"judging {len(jobs)} pairs ({len(done)} cached)", flush=True)
    await asyncio.gather(*(one(p, s) for p, s in jobs))
    return list(done.values())
