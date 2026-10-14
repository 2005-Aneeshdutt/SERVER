import json

import httpx
import respx

from gateway.core.schemas import ChatRequest
from gateway.providers.llamacpp import MAX_DISTILLED_TOKENS, LlamaCppProvider
from gateway.routing.registry import ModelSpec
from tests.helpers import openai_completion

URL = "http://llamacpp:8080/v1/chat/completions"


def spec(task: str | None) -> ModelSpec:
    return ModelSpec(
        alias="distilled-x",
        provider="llamacpp",
        upstream_model="x-v1",
        input_price_per_mtok=0,
        output_price_per_mtok=0,
        context_window=2048,
        tier="cheap",
        distilled_task=task,
    )


REQUEST = ChatRequest.model_validate(
    {
        "model": "groq-gpt-oss-120b",
        "temperature": 0.7,
        "max_tokens": 4096,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": "A very long list of 77 intent labels ..."},
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "my card has not arrived"},
        ],
    }
)


async def test_distilled_requests_are_rewritten_to_the_trained_format() -> None:
    router = respx.Router()
    route = router.post(URL).mock(return_value=httpx.Response(200, json=openai_completion("ok")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler)) as client:
        provider = LlamaCppProvider(client, "http://llamacpp:8080/v1")
        await provider.complete(REQUEST, spec("banking77-intent"))

    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"] == [
        {"role": "system", "content": "task: banking77-intent"},
        {"role": "user", "content": "my card has not arrived"},
    ]
    assert sent["temperature"] == 0 and sent["max_tokens"] == MAX_DISTILLED_TOKENS
    assert sent["cache_prompt"] is True
    assert "reasoning_effort" not in sent  # gpt-oss-specific fields aren't forwarded


async def test_plain_llamacpp_models_pass_requests_through() -> None:
    router = respx.Router()
    route = router.post(URL).mock(return_value=httpx.Response(200, json=openai_completion("ok")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler)) as client:
        await LlamaCppProvider(client, "http://llamacpp:8080/v1").complete(REQUEST, spec(None))
    sent = json.loads(route.calls.last.request.content)
    assert len(sent["messages"]) == 4 and sent["max_tokens"] == 4096
