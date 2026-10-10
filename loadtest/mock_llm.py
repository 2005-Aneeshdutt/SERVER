"""Mock OpenAI-compatible upstream for load tests: fixed latency, no real model, no cost.

MOCK_LATENCY_MS     non-streaming response time (default 100)
MOCK_TTFT_MS        streaming time to first token (default 50)
MOCK_CHUNKS         streamed content chunks (default 20)
MOCK_CHUNK_GAP_MS   gap between chunks (default 5)
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

LATENCY_S = int(os.environ.get("MOCK_LATENCY_MS", "100")) / 1000
TTFT_S = int(os.environ.get("MOCK_TTFT_MS", "50")) / 1000
CHUNKS = int(os.environ.get("MOCK_CHUNKS", "20"))
CHUNK_GAP_S = int(os.environ.get("MOCK_CHUNK_GAP_MS", "5")) / 1000
WORD = "token "

app = FastAPI()


def _usage(completion_tokens: int) -> dict[str, int]:
    return {
        "prompt_tokens": 20,
        "completion_tokens": completion_tokens,
        "total_tokens": 20 + completion_tokens,
    }


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    body: dict[str, Any] = await request.json()
    model = str(body.get("model", "mock"))
    if body.get("stream"):
        return StreamingResponse(_stream(model), media_type="text/event-stream")
    await asyncio.sleep(LATENCY_S)
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": WORD * CHUNKS},
                    "finish_reason": "stop",
                }
            ],
            "usage": _usage(CHUNKS),
        }
    )


async def _stream(model: str) -> AsyncIterator[str]:
    base = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }

    def event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps({**base, **payload})}\n\n"

    await asyncio.sleep(TTFT_S)
    yield event({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]})
    for _ in range(CHUNKS):
        yield event({"choices": [{"index": 0, "delta": {"content": WORD}}]})
        await asyncio.sleep(CHUNK_GAP_S)
    yield event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    yield event({"choices": [], "usage": _usage(CHUNKS)})
    yield "data: [DONE]\n\n"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
