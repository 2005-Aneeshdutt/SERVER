"""Join prompts, both models' answers and judgments into labelled examples."""

import re
from dataclasses import dataclass

from eval import jsonl
from eval.config import CHEAP_ALIAS, GENERATIONS_FILE, JUDGMENTS_FILE, STRONG_ALIAS
from eval.data import load_prompts

_CODE = re.compile(r"```|\bdef |\bclass |\bfunction\b|#include|;\s*$|\bSELECT\b", re.MULTILINE)
_MATH = re.compile(r"\d+\s*[-+*/^=]\s*\d+|\bintegral\b|\bderivative\b|\bprove\b", re.IGNORECASE)


@dataclass(frozen=True)
class Example:
    id: str
    prompt: str
    label: int  # 1 = the cheap model's answer was judged at least as good as the strong one
    cheap_cost: float
    strong_cost: float

    @property
    def has_code(self) -> bool:
        return bool(_CODE.search(self.prompt))

    @property
    def has_math(self) -> bool:
        return bool(_MATH.search(self.prompt))


def label_for(winners: list[str]) -> int:
    """One judgment: cheap wins or ties -> 1. Two (order swapped) that disagree count as a tie
    (the MT-Bench convention: an order-dependent verdict isn't a real preference)."""
    if len(set(winners)) > 1:
        return 1
    return int(winners[0] in ("cheap", "tie"))


def load_examples() -> list[Example]:
    prompts = {p["id"]: p["prompt"] for p in load_prompts()}
    answers = {(g["id"], g["model"]): g for g in jsonl.read(GENERATIONS_FILE)}
    verdicts: dict[str, list[str]] = {}
    for j in jsonl.read(JUDGMENTS_FILE):
        verdicts.setdefault(j["id"], []).append(j["winner"])
    examples = []
    for pid, winners in sorted(verdicts.items()):
        cheap, strong = answers.get((pid, CHEAP_ALIAS)), answers.get((pid, STRONG_ALIAS))
        if cheap is None or strong is None or pid not in prompts:
            continue
        examples.append(
            Example(
                id=pid,
                prompt=prompts[pid],
                label=label_for(winners),
                cheap_cost=float(cheap["cost_usd"]),
                strong_cost=float(strong["cost_usd"]),
            )
        )
    return examples


def swap_consistency() -> tuple[int, int]:
    """(agreeing, total) over prompts judged in both orders."""
    by_id: dict[str, dict[bool, str]] = {}
    for j in jsonl.read(JUDGMENTS_FILE):
        by_id.setdefault(j["id"], {})[bool(j["swapped"])] = j["winner"]
    pairs = [d for d in by_id.values() if len(d) == 2]
    return sum(d[False] == d[True] for d in pairs), len(pairs)
