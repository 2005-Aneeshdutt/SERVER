"""Traffic replayer: drives the gateway with public-dataset traffic, labelled simulated.

    uv run --group distill python -m distill.replay prepare --n 300   # data/tasks/*.jsonl
    uv run --group distill python -m distill.replay key               # simulated, opted-in key
    uv run --group distill python -m distill.replay run --limit 100   # send (resumable)

- Every request uses a key flagged `simulated` (and sends `X-Gateway-Simulated: 1`), so
  simulated traffic is labelled in every event, row and file.
- The key may use only the free-tier strong model and the $0 replay model, so a fallback can
  never reach a paid provider ($0 budget).
- Resumable: every successful request is appended to data/replay/sent.jsonl and skipped on the
  next run. Groq's free tier allows 1,000 requests/day and 8,000 tokens/min for gpt-oss-120b
  (measured from its rate-limit headers on 2026-10-13), so a full replay takes days. Pacing
  is set with --rpm, and 429s are retried after Retry-After.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from distill.datasets import (
    DATA,
    TASKS,
    TaskExample,
    banking77,
    fewnerd,
    noise,
    read,
    wikisql,
    write,
)

REPLAY = DATA / "replay"
KEY_FILE = REPLAY / "key.txt"
KEY_ID_FILE = REPLAY / "key_id.txt"  # reports select this key's events only
SENT_FILE = REPLAY / "sent.jsonl"
STRONG = "groq-gpt-oss-120b"
NOISE_ALIAS = "sim-llama-70b-replay"
TASK_NAMES = (banking77.TASK, wikisql.TASK, fewnerd.TASK, noise.TASK)


@dataclass(frozen=True)
class Target:
    alias: str
    max_tokens: int
    paced: bool  # counts against the free-tier rate limit


TARGETS = {
    banking77.TASK: Target(STRONG, 512, True),
    wikisql.TASK: Target(STRONG, 512, True),
    fewnerd.TASK: Target(STRONG, 512, True),
    noise.TASK: Target(NOISE_ALIAS, 1024, False),
}


def prepare(n: int, seed: int, tasks: list[str]) -> None:
    if banking77.TASK in tasks:
        write(banking77.TASK, banking77.load(n, seed))
    if wikisql.TASK in tasks:
        examples, tables = wikisql.load(n, seed)
        write(wikisql.TASK, examples)
        wikisql.save_tables(tables, TASKS / "wikisql_tables.jsonl")
    if fewnerd.TASK in tasks:
        write(fewnerd.TASK, fewnerd.load(n, seed))
    if noise.TASK in tasks:
        write(noise.TASK, noise.load(n * 2))
    for task in tasks:
        print(f"{task}: {len(read(task))} examples -> {TASKS / f'{task}.jsonl'}")


def _admin_token() -> str:
    if token := os.environ.get("ADMIN_TOKEN"):
        return token
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("ADMIN_TOKEN must be set (env or .env)")


def create_key(gateway: str) -> None:
    response = httpx.post(
        f"{gateway}/admin/keys",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "name": "replayer (simulated traffic)",
            "allowed_models": [STRONG, NOISE_ALIAS],  # no paid fallback can ever be reached
            "distill_opt_in": True,
            "simulated": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    REPLAY.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(response.json()["key"], encoding="utf-8")
    KEY_ID_FILE.write_text(response.json()["id"], encoding="utf-8")
    print(f"created simulated, opted-in key {response.json()['key_prefix']}... -> {KEY_FILE}")


def _sent() -> set[str]:
    if not SENT_FILE.exists():
        return set()
    rows = [json.loads(line) for line in SENT_FILE.read_text("utf-8").splitlines() if line]
    return {r["example_id"] for r in rows if r["status"] == 200}


def _interleave(tasks: list[str], limit: int, done: set[str]) -> list[TaskExample]:
    """Round-robin across tasks, like mixed production traffic."""
    queues = [[e for e in read(t) if e.example_id not in done][:limit] for t in tasks]
    out: list[TaskExample] = []
    for i in range(max(map(len, queues), default=0)):
        out.extend(q[i] for q in queues if i < len(q))
    return out


def _body(example: TaskExample, target: Target) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": target.alias,
        "messages": example.messages,
        "temperature": 0,
        "max_tokens": target.max_tokens,
    }
    if target.alias == STRONG:
        body["reasoning_effort"] = "low"  # gpt-oss: keeps reasoning tokens (and TPM use) down
    if example.response_format:
        body["response_format"] = example.response_format
    return body


def send(client: httpx.Client, example: TaskExample, target: Target) -> httpx.Response:
    for attempt in range(6):
        response = client.post("/v1/chat/completions", json=_body(example, target))
        if response.status_code not in (429, 502, 503, 504):
            return response
        wait = float(response.headers.get("retry-after") or 30 * (attempt + 1))
        print(f"  {response.status_code}; waiting {wait:.0f}s", flush=True)
        time.sleep(min(wait, 300))
    return response


def run(gateway: str, tasks: list[str], limit: int, rpm: float) -> None:
    if not KEY_FILE.exists():
        sys.exit(f"no replay key: run `python -m distill.replay key` first ({KEY_FILE})")
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    todo = _interleave(tasks, limit, _sent())
    print(f"{len(todo)} requests to send ({', '.join(tasks)}); paced at {rpm} rpm", flush=True)
    stats: Counter[str] = Counter()
    headers = {"Authorization": f"Bearer {key}", "X-Gateway-Simulated": "1"}
    interval = 60.0 / rpm
    next_paced = 0.0
    with httpx.Client(base_url=gateway, headers=headers, timeout=180) as client:
        for i, example in enumerate(todo, 1):
            target = TARGETS[example.task]
            if target.paced:
                time.sleep(max(0.0, next_paced - time.monotonic()))
                next_paced = time.monotonic() + interval
            response = send(client, example, target)
            stats[f"{example.task}:{response.status_code}"] += 1
            with SENT_FILE.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "example_id": example.example_id,
                            "task": example.task,
                            "status": response.status_code,
                            "request_id": response.headers.get("x-gateway-request-id"),
                            "ts": time.time(),
                        }
                    )
                    + "\n"
                )
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} {dict(stats)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="distill.replay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="download datasets and write data/tasks/*.jsonl")
    p.add_argument("--n", type=int, default=300, help="examples per task")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--tasks", default=",".join(TASK_NAMES))
    k = sub.add_parser("key", help="create the simulated, opted-in replay key")
    k.add_argument("--gateway", default="http://localhost:8000")
    r = sub.add_parser("run", help="send traffic through the gateway (resumable)")
    r.add_argument("--gateway", default="http://localhost:8000")
    r.add_argument("--tasks", default=",".join(TASK_NAMES))
    r.add_argument("--limit", type=int, default=100, help="max new requests per task")
    r.add_argument("--rpm", type=float, default=10.0, help="pace for free-tier requests")
    args = parser.parse_args()
    REPLAY.mkdir(parents=True, exist_ok=True)
    if args.cmd == "prepare":
        prepare(args.n, args.seed, args.tasks.split(","))
    elif args.cmd == "key":
        create_key(args.gateway)
    else:
        run(args.gateway, args.tasks.split(","), args.limit, args.rpm)


if __name__ == "__main__":
    main()
