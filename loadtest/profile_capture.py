"""CPU per request with distillation capture off / building-only / on, measured in-process.

Runs INSIDE the gateway container (it needs the compose network: Postgres, Redis, Kafka and the
mock LLM from the load-test overlay), so container overhead is included and other containers'
CPU is not:

    docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml \\
        --profile distill up -d mock-llm api kafka
    docker compose cp loadtest/profile_capture.py api:/tmp/profile_capture.py
    docker compose exec -T api python /tmp/profile_capture.py <opted-in gw- key> off
    docker compose exec -T api python /tmp/profile_capture.py <key> null   # build, don't send
    docker compose exec -T api python /tmp/profile_capture.py <key> on
    ... add `--rps 30` for a constant arrival rate (30 s), like the k6 runs
    ... add `--profile` to also dump cProfile stats to /tmp/capture_<mode>.pstats

Drives the real app through httpx's ASGI transport (20 concurrent, 2,000 requests, 2 KB prompts
dense with emails and phone numbers: the worst case for PII scrubbing) and reports process CPU
time per request. This is how ADR 0015's cost breakdown was measured.
"""

import asyncio
import cProfile
import os
import sys
import time
from typing import Any

import httpx

import gateway.capture.kafka as kafka_sender
from gateway.app import create_app
from gateway.core.config import get_settings

FILLER = (
    "Hi, my card payment was declined twice today. You can reach me at jane.doe@example.com "
    "or +1 415 555 0132. "
)


def configure(mode: str) -> None:
    os.environ["CAPTURE_ENABLED"] = "false" if mode == "off" else "true"
    os.environ.setdefault("KAFKA_BOOTSTRAP", "kafka:9092")
    get_settings.cache_clear()
    if mode == "null":  # events are scrubbed and serialized, then discarded: Kafka excluded

        async def noop(*_: Any, **__: Any) -> None:
            return None

        kafka_sender.KafkaSender.start = noop  # type: ignore[method-assign]
        kafka_sender.KafkaSender.stop = noop  # type: ignore[method-assign]
        kafka_sender.KafkaSender.send = noop  # type: ignore[method-assign]


async def measure(key: str, n: int, profile: cProfile.Profile | None, rps: float) -> float:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw", headers=headers
        ) as client:
            sem = asyncio.Semaphore(20)

            async def one(i: int) -> None:
                body = {
                    "model": "gpt-4o-mini",
                    "temperature": 0.7,
                    "max_tokens": 64,
                    "messages": [
                        {"role": "system", "content": "You are a support assistant for a bank."},
                        {"role": "user", "content": (f"t {i} " + FILLER * 20)[:2000]},
                    ],
                }
                async with sem:
                    response = await client.post("/v1/chat/completions", json=body)
                    response.raise_for_status()

            await asyncio.gather(*(one(i) for i in range(100)))  # warm up
            started = time.process_time()
            if profile is not None:
                profile.enable()
            if rps:  # constant arrival rate, like k6, instead of as fast as possible
                tasks = []
                for i in range(n):
                    tasks.append(asyncio.create_task(one(i)))
                    await asyncio.sleep(1 / rps)
                await asyncio.gather(*tasks)
            else:
                await asyncio.gather(*(one(i) for i in range(n)))
            if profile is not None:
                profile.disable()
            cpu = time.process_time() - started
    return cpu / n * 1000


def main() -> None:
    key, mode = sys.argv[1], sys.argv[2]
    configure(mode)
    profile = cProfile.Profile() if "--profile" in sys.argv else None
    rps = float(sys.argv[sys.argv.index("--rps") + 1]) if "--rps" in sys.argv else 0.0
    n = int(rps * 30) if rps else 2000
    ms = asyncio.run(measure(key, n, profile, rps))
    pace = f"{rps:g} rps" if rps else "max speed"
    print(f"capture={mode}: {ms:.2f} ms CPU/request ({n} requests, {pace})")
    if profile is not None:
        profile.dump_stats(f"/tmp/capture_{mode}.pstats")


if __name__ == "__main__":
    main()
