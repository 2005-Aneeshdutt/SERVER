"""Banking77 (PolyAI, CC-BY-4.0): classify a bank customer's message into one of 77 intents.

Source: legacy-datasets/banking77 (the official copy, served as Parquet). Checkable: the answer
must be exactly one label from a closed set, and gold labels exist.
"""

from typing import Any

from distill.datasets import TaskExample, example_id
from distill.datasets.hf import features, sample_rows

TASK = "banking77-intent"
DATASET, CONFIG, SPLIT = "legacy-datasets/banking77", "default", "train"


def label_names() -> list[str]:
    label = next(f for f in features(DATASET, CONFIG, SPLIT) if f["name"] == "label")
    names: list[str] = label["type"]["names"]
    return names


def system_prompt(labels: list[str]) -> str:
    return (
        "You are the intent classifier for a bank's customer-support inbox. "
        "Classify the customer's message into exactly one of these intents:\n"
        + "\n".join(labels)
        + "\n\nReply with the intent label only, exactly as written above, and nothing else."
    )


def load(n: int, seed: int = 7) -> list[TaskExample]:
    labels = label_names()
    system = system_prompt(labels)
    out = []
    seen: set[str] = set()

    def keep(row: dict[str, Any]) -> bool:
        return bool(str(row["text"]).strip())

    for row in sample_rows(DATASET, CONFIG, SPLIT, n * 2, seed, keep):
        text = str(row["text"]).strip()
        eid = example_id(TASK, text)
        if eid in seen:
            continue
        seen.add(eid)
        out.append(
            TaskExample(
                task=TASK,
                example_id=eid,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
                gold=labels[int(row["label"])],
            )
        )
        if len(out) >= n:
            break
    return out
