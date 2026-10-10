"""Generate cheap- and strong-model answers through the gateway's own provider adapters."""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

from eval import jsonl
from gateway.core.errors import BadRequest, ProviderError
from gateway.core.schemas import ChatRequest, ChatResponse
from gateway.providers.base import Provider
from gateway.routing.registry import ModelSpec

MAX_ATTEMPTS = 4


def billed_cost(response: ChatResponse, spec: ModelSpec) -> str:
    """OpenRouter reports the exact billed cost; fall back to registry prices otherwise."""
    usage = response.usage
    if usage is None:
        return "0"
    reported = (usage.model_extra or {}).get("cost")
    if reported is not None:
        return str(Decimal(str(reported)))
    return str(spec.cost_usd(usage.prompt_tokens, usage.completion_tokens))


async def complete_with_retries(
    provider: Provider, request: ChatRequest, spec: ModelSpec
) -> ChatResponse | None:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await provider.complete(request, spec)
        except BadRequest:
            return None  # e.g. the prompt trips a provider filter: skip, don't retry
        except ProviderError:
            await asyncio.sleep(2 * 2**attempt)
    return None


async def generate(
    prompts: list[dict[str, str]],
    spec: ModelSpec,
    provider: Provider,
    cache: Path,
    max_tokens: int,
    concurrency: int = 8,
) -> dict[str, dict[str, Any]]:
    """Answers for every prompt from one model, keyed by prompt id. Resumes from the cache."""
    done = {r["id"]: r for r in jsonl.read(cache) if r["model"] == spec.alias}
    todo = [p for p in prompts if p["id"] not in done]
    appender = jsonl.Appender(cache)
    semaphore = asyncio.Semaphore(concurrency)
    progress = {"n": 0}

    async def one(item: dict[str, str]) -> None:
        request = ChatRequest.model_validate(
            {
                "model": spec.alias,
                "messages": [{"role": "user", "content": item["prompt"]}],
                "temperature": 0,
                "max_tokens": max_tokens,
            }
        )
        async with semaphore:
            response = await complete_with_retries(provider, request, spec)
        if response is None or not response.choices:
            return
        usage = response.usage
        row = {
            "id": item["id"],
            "model": spec.alias,
            "answer": response.choices[0].message.content or "",
            "finish_reason": response.choices[0].finish_reason,
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "cost_usd": billed_cost(response, spec),
        }
        done[item["id"]] = row
        await appender.append(row)
        progress["n"] += 1
        if progress["n"] % 100 == 0:
            print(f"  {spec.alias}: {progress['n']}/{len(todo)}", flush=True)

    if todo:
        print(f"generating {len(todo)} answers with {spec.alias} ({len(done)} cached)", flush=True)
    await asyncio.gather(*(one(p) for p in todo))
    return done
