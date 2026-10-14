"""Output validators for distilled tasks: cheap, deterministic checks that an answer has the
right *shape*. Used offline (the dataset builder drops teacher outputs that fail) and online
(a distilled answer that fails is re-served by the strong model, and counts towards rollback).

A spec is plain JSON so it can live in Redis and in dataset manifests:
    {"type": "label_set", "labels": [...]}      exactly one known label
    {"type": "json_keys", "keys": [...]}        a JSON object with these keys, list-of-str values
    {"type": "sql_select"}                      a single SELECT statement
"""

import contextlib
import json
import re
from typing import Any

_SELECT = re.compile(r"^\s*select\b", re.IGNORECASE)


def normalize_label(text: str) -> str:
    return text.strip().strip("`'\".").strip()


def validate(spec: dict[str, Any], output: str | None) -> bool:
    if output is None:
        return False
    kind = spec.get("type")
    if kind == "label_set":
        return normalize_label(output) in set(spec["labels"])
    if kind == "json_keys":
        try:
            value = json.loads(output)
        except ValueError:
            return False
        return (
            isinstance(value, dict)
            and set(value) == set(spec["keys"])
            and all(
                isinstance(v, list) and all(isinstance(x, str) for x in v) for v in value.values()
            )
        )
    if kind == "sql_select":
        statement = output.strip().rstrip(";").strip()
        return bool(_SELECT.match(statement)) and ";" not in statement
    return False


def detect(outputs: list[str], min_label_count: int = 2) -> dict[str, Any] | None:
    """Infer a validator from a task's observed (teacher) outputs, or None if the outputs have
    no checkable shape. Requires >= 95% of outputs to fit."""
    if not outputs:
        return None
    n = len(outputs)
    labels: dict[str, int] = {}
    for o in outputs:
        label = normalize_label(o)
        labels[label] = labels.get(label, 0) + 1
    single_line = sum("\n" not in o.strip() and len(o.strip()) <= 64 for o in outputs)
    if len(labels) <= max(100, n // 10) and single_line >= 0.95 * n:
        allowed = sorted(k for k, c in labels.items() if c >= min_label_count and k)
        covered = sum(labels[k] for k in allowed)
        if covered >= 0.95 * n:
            return {"type": "label_set", "labels": allowed}
    parsed = []
    for o in outputs:
        with contextlib.suppress(ValueError):
            parsed.append(json.loads(o))
    objects = [p for p in parsed if isinstance(p, dict)]
    if len(objects) >= 0.95 * n:
        keys = max(
            (tuple(sorted(o)) for o in objects),
            key=lambda k: sum(tuple(sorted(o)) == k for o in objects),
        )
        spec = {"type": "json_keys", "keys": list(keys)}
        if sum(validate(spec, o) for o in outputs) >= 0.95 * n:
            return spec
    if sum(validate({"type": "sql_select"}, o) for o in outputs) >= 0.95 * n:
        return {"type": "sql_select"}
    return None
