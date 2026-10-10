"""Helpers for translating OpenAI message content into other providers' formats."""

import json
import re
from typing import Any

from gateway.core.schemas import ChatMessage

_DATA_URL = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$", re.DOTALL)


def content_parts(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize OpenAI content (string or part list) to a part list."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return content


def plain_text(content: str | list[dict[str, Any]] | None) -> str:
    return "".join(
        str(p.get("text", "")) for p in content_parts(content) if p.get("type") == "text"
    )


def parse_data_url(url: str) -> tuple[str, str] | None:
    """`data:image/png;base64,AAAA` -> ("image/png", "AAAA")."""
    match = _DATA_URL.match(url)
    if match is None:
        return None
    return match.group("mime"), match.group("data")


def image_url_of(part: dict[str, Any]) -> str:
    image = part.get("image_url")
    if isinstance(image, dict):
        return str(image.get("url", ""))
    return str(image or "")


def system_prompt(messages: list[ChatMessage]) -> str | None:
    texts = [plain_text(m.content) for m in messages if m.role in ("system", "developer")]
    joined = "\n\n".join(t for t in texts if t)
    return joined or None


def parse_arguments(arguments: str) -> dict[str, Any]:
    """Tool-call arguments arrive as a JSON string; native APIs want an object."""
    if not arguments:
        return {}
    try:
        value = json.loads(arguments)
    except ValueError:
        return {"_raw": arguments}
    return value if isinstance(value, dict) else {"value": value}


def function_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Extract the `function` objects from OpenAI-style tool definitions."""
    return [t["function"] for t in tools or [] if t.get("type") == "function" and "function" in t]
