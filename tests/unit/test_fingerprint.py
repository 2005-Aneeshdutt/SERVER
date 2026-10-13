from gateway.capture.fingerprint import system_fingerprint, template_fingerprint, template_shape
from gateway.core.schemas import ChatMessage


def msgs(system: str | None, user: str) -> list[ChatMessage]:
    out = [ChatMessage(role="system", content=system)] if system is not None else []
    return [*out, ChatMessage(role="user", content=user)]


def test_system_fingerprint_ignores_case_and_whitespace() -> None:
    a = system_fingerprint(msgs("Classify the intent.\n  Reply with one label.", "x"))
    b = system_fingerprint(msgs("classify the intent. reply with one label.", "y"))
    assert a is not None and a == b


def test_different_system_prompts_differ_and_missing_is_none() -> None:
    assert system_fingerprint(msgs("Task A", "x")) != system_fingerprint(msgs("Task B", "x"))
    assert system_fingerprint(msgs(None, "x")) is None


def test_template_shape_keeps_field_labels_and_drops_content() -> None:
    text = "Table: CREATE TABLE t (a TEXT)\nQuestion: How many rows?\nmore text\nand more"
    assert template_shape(text) == "table|question|_"


def test_same_template_with_different_content_matches() -> None:
    a = template_fingerprint(msgs("s", "Table: t1\nQuestion: who won?"))
    b = template_fingerprint(msgs("s", "Table: other\nQuestion: when?"))
    assert a == b


def test_a_changed_prompt_format_changes_the_template_fingerprint() -> None:
    before = template_fingerprint(msgs("s", "Message: my card is lost"))
    after = template_fingerprint(msgs("s", "Subject: help\nBody: my card is lost"))
    assert before != after
