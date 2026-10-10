"""Approximate token counting for when a provider doesn't report usage.

tiktoken's o200k encoding is exact for recent OpenAI models and a reasonable approximation for
everyone else. Loading it can touch disk/network, so it happens once at startup (`warm_up`, run in a
thread). Until it's loaded we fall back to ~4 chars/token instead of blocking a request.
"""

import logging
from collections.abc import Sequence
from typing import Any

from gateway.core.schemas import ChatMessage

log = logging.getLogger(__name__)

_ENCODING_NAME = "o200k_base"
_PER_MESSAGE_OVERHEAD = 4
_PER_REQUEST_OVERHEAD = 3
_encoding: Any = None


def warm_up() -> None:
    """Blocking: load the tokenizer. Call via `asyncio.to_thread` at startup."""
    global _encoding
    try:
        import tiktoken

        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
    except Exception:
        log.warning("tiktoken unavailable; using chars/4 token estimates", exc_info=True)


def count_text(text: str) -> int:
    if not text:
        return 0
    if _encoding is None:
        return max(1, len(text) // 4)
    return len(_encoding.encode(text, disallowed_special=()))


def message_text(message: ChatMessage) -> str:
    parts: list[str] = []
    if isinstance(message.content, str):
        parts.append(message.content)
    elif message.content:
        parts.extend(str(p.get("text", "")) for p in message.content if p.get("type") == "text")
    for call in message.tool_calls or []:
        parts.append(call.function.name)
        parts.append(call.function.arguments)
    return "\n".join(parts)


def count_messages(messages: Sequence[ChatMessage]) -> int:
    """Prompt-token estimate. Images are not counted (no portable way to do so)."""
    total = _PER_REQUEST_OVERHEAD
    for message in messages:
        total += _PER_MESSAGE_OVERHEAD + count_text(message_text(message))
    return total
