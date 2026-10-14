import json
from pathlib import Path
from typing import Any

from distill.build import build
from distill.discover import discover
from gateway.routing.registry import ModelRegistry

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ModelRegistry.from_yaml(ROOT / "models.yaml")


def row(
    i: int, fp: str | None, text: str, out: str, alias: str = "groq-gpt-oss-120b"
) -> dict[str, Any]:
    return {
        "event_id": f"e{i}",
        "ts": i,
        "simulated": True,
        "system_fingerprint": fp,
        "messages_json": json.dumps(
            [{"role": "system", "content": "s"}, {"role": "user", "content": text}]
        ),
        "response_text": out,
        "served_alias": alias,
        "status": "ok",
    }


def traffic() -> dict[str, dict[str, Any]]:
    rows = [row(i, "intent", f"message {i}", ["card_lost", "refund"][i % 2]) for i in range(60)]
    rows += [row(100 + i, None, f"chat {i}", f"long answer {i}\nmore") for i in range(40)]
    rows += [
        row(200 + i, "essay", f"essay {i}", f"Essay {i} text\nparagraph two") for i in range(20)
    ]
    return {r["event_id"]: r for r in rows}


def test_discovery_ranks_the_checkable_repeated_task_first() -> None:
    tasks = discover(traffic(), REGISTRY, {"intent": "bank-intent"})
    top = tasks[0]
    assert top["task"] == "bank-intent"
    assert top["validator"] == {"type": "label_set", "labels": ["card_lost", "refund"]}
    assert top["stability"] == 1.0 and top["strong_share"] == 1.0
    essay = next(t for t in tasks if t.get("system_fingerprint") == "essay")
    assert essay["score"] == 0.0  # free text: no validator, never a distillation candidate
    assert tasks[-1] == {"task": "(no system prompt)", "events": 40, "score": 0.0}


def test_builder_filters_dedupes_and_splits_deterministically() -> None:
    content = traffic()
    content["e1"]["response_text"] = "not_a_label"  # fails the validator
    content["e2"]["served_alias"] = "gemini-flash"  # served by a fallback, not the source
    content["e3"]["messages_json"] = content["e5"]["messages_json"]  # duplicate input
    task = discover(content, REGISTRY, {"intent": "bank-intent"})[0]
    pii = {"e4": 1}  # scrubbed prompt: dropped

    examples, manifest = build(task, content, pii, gold={"message 6": "refund"})

    c = manifest["counts"]
    assert (c["captured"], c["served_by_source"], c["not_scrubbed"]) == (60, 59, 58)
    assert c["validator_passed"] == 57 and c["deduplicated"] == 56
    assert c["train"] + c["test"] == 56 and c["test"] > 0
    assert all(e["system"] == "task: bank-intent" for e in examples)
    assert next(e for e in examples if e["input"] == "message 6")["gold"] == "refund"
    _, manifest2 = build(task, content, pii, gold={"message 6": "refund"})
    assert manifest2["data_hash"] == manifest["data_hash"]  # deterministic
