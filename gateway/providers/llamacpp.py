"""Self-hosted distilled models served by llama.cpp's OpenAI-compatible server (M8).

For a distilled alias (`distilled_task` set in models.yaml), the request is rewritten into the
exact format the model was fine-tuned on (gateway/distill/prompt.py): the long task
instructions are replaced by a short task tag, and only the last user message is kept.
Sampling is greedy, and output is capped, since a narrow task's answers are short.
"""

from typing import Any

import httpx

from gateway.core.schemas import ChatRequest
from gateway.distill.prompt import distilled_messages, last_user_text
from gateway.providers.openai_compat import OpenAICompatibleProvider
from gateway.routing.registry import ModelSpec

MAX_DISTILLED_TOKENS = 256


class LlamaCppProvider(OpenAICompatibleProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        super().__init__("llamacpp", client, base_url, None)

    def _payload(self, req: ChatRequest, model: ModelSpec, stream: bool) -> dict[str, Any]:
        body = super()._payload(req, model, stream)
        if model.distilled_task is None:
            return body
        cap = req.output_token_cap() or MAX_DISTILLED_TOKENS
        payload: dict[str, Any] = {
            "model": body["model"],
            "messages": distilled_messages(model.distilled_task, last_user_text(req.messages)),
            "temperature": 0,
            "max_tokens": min(cap, MAX_DISTILLED_TOKENS),
            "cache_prompt": True,  # llama.cpp: reuse the KV cache for the shared prefix
        }
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload
