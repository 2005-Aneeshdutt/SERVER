"""Few-NERD (CC-BY-SA-4.0): extract person / organization / location names as JSON.

Source: DFKI-SLT/few-nerd, "supervised" config, coarse tags. Checkable: the reply must be JSON
with three string lists; gold entity sets exist (scored with set F1).

Few-NERD uses IO tagging (no B-/I- prefixes), so two adjacent entities of the same type merge
into one gold span. That's rare and affects every model equally; noted, not corrected.
"""

import json
import re
from typing import Any

from distill.datasets import TaskExample, example_id
from distill.datasets.hf import features, sample_rows

TASK = "fewnerd-entities"
DATASET, CONFIG, SPLIT = "DFKI-SLT/few-nerd", "supervised", "train"
TYPES = ("person", "organization", "location")

SYSTEM = (
    "You extract named entities from text for a search index. Find every person, organization "
    "and location name in the user's text. Reply with a JSON object only, in exactly this form: "
    '{"person": [...], "organization": [...], "location": [...]}. Copy each name exactly as it '
    "appears in the text. Use an empty list when there are none."
)

_NO_SPACE_BEFORE = re.compile(r" ([.,;:!?)\]'%])")
_NO_SPACE_AFTER = re.compile(r"([(\[$]) ")


def detokenize(tokens: list[str]) -> str:
    text = " ".join(t for t in tokens if t not in ("``", "''"))
    text = _NO_SPACE_BEFORE.sub(r"\1", text)
    return _NO_SPACE_AFTER.sub(r"\1", text)


def gold_entities(tokens: list[str], tags: list[int], names: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {t: [] for t in TYPES}
    span: list[str] = []
    current = "O"
    for token, tag in [*zip(tokens, tags, strict=True), ("", 0)]:
        name = names[tag]
        if name != current:
            if current in out and span:
                entity = detokenize(span)
                if entity not in out[current]:
                    out[current].append(entity)
            span, current = [], name
        if name != "O":
            span.append(token)
    return out


def load(n: int, seed: int = 7) -> list[TaskExample]:
    names: list[str] = next(f for f in features(DATASET, CONFIG, SPLIT) if f["name"] == "ner_tags")[
        "type"
    ]["feature"]["names"]
    idx = {name: i for i, name in enumerate(names)}

    def keep(row: dict[str, Any]) -> bool:
        tags = row["ner_tags"]
        return 8 <= len(row["tokens"]) <= 60 and any(tags.count(idx[t]) for t in TYPES)

    out, seen = [], set()
    for row in sample_rows(DATASET, CONFIG, SPLIT, n * 2, seed, keep):
        text = detokenize(row["tokens"])
        eid = example_id(TASK, text)
        if eid in seen:
            continue
        seen.add(eid)
        gold = gold_entities(row["tokens"], row["ner_tags"], names)
        out.append(
            TaskExample(
                task=TASK,
                example_id=eid,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
                gold=json.dumps(gold, sort_keys=True),
                response_format={"type": "json_object"},
            )
        )
        if len(out) >= n:
            break
    return out
