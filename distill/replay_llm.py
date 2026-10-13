"""OpenAI-compatible server that replays recorded answers (simulated noise traffic, $0).

    uvicorn distill.replay_llm:app --port 9100     # REPLAY_DATA_DIR defaults to eval/data

Looks up the last user message among the M3 prompts and returns the answer Llama 3.3 70B gave
to it, with the recorded token counts. Unknown prompts get a 400, so nothing is ever invented.
"""

import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from distill.datasets.noise import recorded_answers

app = FastAPI()
ANSWERS = recorded_answers(Path(os.environ.get("REPLAY_DATA_DIR", "eval/data")))


@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body: dict[str, Any] = await request.json()
    if body.get("stream"):
        return _error("replay server is non-streaming only")
    users = [m.get("content") for m in body.get("messages", []) if m.get("role") == "user"]
    row = ANSWERS.get(str(users[-1])) if users else None
    if row is None:
        return _error("no recorded answer for this prompt")
    prompt, completion = int(str(row["prompt_tokens"])), int(str(row["completion_tokens"]))
    return JSONResponse(
        {
            "id": f"chatcmpl-replay-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(body.get("model", "replay")),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": row["answer"]},
                    "finish_reason": row.get("finish_reason") or "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "recordings": len(ANSWERS)}


def _error(message: str) -> JSONResponse:
    body = {"error": {"message": message, "type": "invalid_request_error", "code": "replay"}}
    return JSONResponse(body, status_code=400)
