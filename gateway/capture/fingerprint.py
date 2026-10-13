"""Cheap prompt fingerprints for grouping traffic without embeddings.

- system fingerprint: hash of the normalized system/developer instructions. Apps that run a
  narrow task send the same system prompt every time, so this identifies the task cheaply.
  M10 matches distilled tasks on it (a dict lookup on the hot path).
- template fingerprint: hash of the user message's *shape*: the labelled fields of a template
  ("question:", "table:") with the content dropped. It separates two templates that share a
  system prompt, and changes when an app changes its prompt format (the M12 drift demo).
"""

import hashlib
import re
from collections.abc import Sequence

from gateway.core.schemas import ChatMessage

_WS = re.compile(r"\s+")
_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z _-]{0,30}):")


def _text(message: ChatMessage) -> str:
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(str(part.get("text", "")) for part in content if part.get("type") == "text")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def system_fingerprint(messages: Sequence[ChatMessage]) -> str | None:
    parts = [_text(m) for m in messages if m.role in ("system", "developer")]
    if not parts:
        return None
    return _digest(_WS.sub(" ", "\n".join(parts)).strip().lower())


def template_shape(text: str) -> str:
    """'Table: ...\\nQuestion: ...' -> 'table|question'. Unlabelled lines collapse to '_'."""
    shape: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _FIELD.match(line)
        token = match.group(1).strip().lower() if match else "_"
        if not (token == "_" and shape and shape[-1] == "_"):
            shape.append(token)
    return "|".join(shape)


def template_fingerprint(messages: Sequence[ChatMessage]) -> str | None:
    users = [_text(m) for m in messages if m.role == "user"]
    if not users:
        return None
    return _digest(template_shape(users[-1]))
