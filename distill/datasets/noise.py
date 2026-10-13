"""Background "noise" traffic: the 2,000 diverse Chatbot Arena prompts from M3 (Apache-2.0).

They make task discovery non-trivial: the three narrow tasks have to stand out from general
chat traffic. They're answered by `sim-llama-70b-replay`, which returns the answers Llama 3.3
70B really gave in the M3 eval (eval/data/generations.jsonl), so no model is called and nothing
is spent. Like all replayed traffic, it's labelled simulated.
"""

import json
from pathlib import Path

from distill.datasets import TaskExample

TASK = "noise-lmsys"
EVAL_DATA = Path("eval/data")
STRONG = "or-llama-70b"


def recorded_answers(eval_data: Path = EVAL_DATA) -> dict[str, dict[str, object]]:
    """prompt text -> the strong model's recorded generation."""
    prompts = {
        row["id"]: row["prompt"]
        for row in map(json.loads, (eval_data / "prompts.jsonl").read_text("utf-8").splitlines())
    }
    out: dict[str, dict[str, object]] = {}
    for row in map(json.loads, (eval_data / "generations.jsonl").read_text("utf-8").splitlines()):
        if row["model"] == STRONG and row["id"] in prompts:
            out[prompts[row["id"]]] = row
    return out


def load(n: int) -> list[TaskExample]:
    answers = recorded_answers()
    out = []
    for prompt, row in list(answers.items())[:n]:
        out.append(
            TaskExample(
                task=TASK,
                example_id=str(row["id"]),
                messages=[{"role": "user", "content": prompt}],
                gold="",  # open-ended chat: no ground truth
            )
        )
    return out
