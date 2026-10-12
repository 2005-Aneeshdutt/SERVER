"""M1 acceptance check against REAL providers (costs a few free-tier tokens).

    GATEWAY_KEY=gw-... uv run python scripts/smoke_openai_sdk.py [--base-url http://localhost:8000/v1]

Uses the official OpenAI SDK, streaming and non-streaming, against Groq and Gemini aliases
(or any others via --aliases, e.g. ollama-llama-8b for a free local run).
"""

import argparse
import asyncio
import os
import sys
import time

import openai
from openai.types.chat import ChatCompletionMessageParam

DEFAULT_ALIASES = "groq-gpt-oss-20b,gemini-flash"
# Reasoning models (gpt-oss) spend output tokens thinking before they answer.
MAX_TOKENS = 256
# Exercise the real providers, not the response cache.
NO_CACHE = {"X-Gateway-Cache": "skip"}
PROMPT: list[ChatCompletionMessageParam] = [
    {"role": "user", "content": "Reply with exactly: gateway ok"}
]


async def non_streaming(client: openai.AsyncOpenAI, alias: str) -> str:
    started = time.perf_counter()
    raw = await client.chat.completions.with_raw_response.create(
        model=alias,
        messages=PROMPT,
        max_tokens=MAX_TOKENS,
        temperature=0,
        extra_headers=NO_CACHE,
    )
    completion = raw.parse()
    elapsed = (time.perf_counter() - started) * 1000
    tokens = completion.usage.total_tokens if completion.usage else "?"
    text = completion.choices[0].message.content
    return (
        f"{alias:16} non-stream  {elapsed:7.0f}ms  provider={raw.headers.get('x-gateway-provider')}"
        f"  cost=${raw.headers.get('x-gateway-cost-usd')}  tokens={tokens}  -> {text!r}"
    )


async def streaming(client: openai.AsyncOpenAI, alias: str) -> str:
    started = time.perf_counter()
    stream = await client.chat.completions.create(
        model=alias,
        messages=PROMPT,
        max_tokens=MAX_TOKENS,
        temperature=0,
        extra_headers=NO_CACHE,
        stream=True,
        stream_options={"include_usage": True},
    )
    text, chunks, ttft, usage = "", 0, None, None
    async for chunk in stream:
        chunks += 1
        if chunk.choices and chunk.choices[0].delta.content:
            ttft = ttft or (time.perf_counter() - started) * 1000
            text += chunk.choices[0].delta.content
        usage = chunk.usage or usage
    elapsed = (time.perf_counter() - started) * 1000
    return (
        f"{alias:16} stream      {elapsed:7.0f}ms  ttft={ttft or 0:.0f}ms  chunks={chunks}"
        f"  tokens={usage.total_tokens if usage else '?'}  -> {text!r}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    parser.add_argument("--aliases", default=DEFAULT_ALIASES, help="comma-separated")
    args = parser.parse_args()
    key = os.environ.get("GATEWAY_KEY")
    if not key:
        print("set GATEWAY_KEY (create one with: python -m gateway.cli create-key --name smoke)")
        return 2
    client = openai.AsyncOpenAI(api_key=key, base_url=args.base_url, max_retries=0)
    failures = 0
    for alias in args.aliases.split(","):
        for check in (non_streaming, streaming):
            try:
                print("PASS", await check(client, alias))
            except openai.APIError as exc:
                failures += 1
                print(f"FAIL {alias:16} {check.__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
