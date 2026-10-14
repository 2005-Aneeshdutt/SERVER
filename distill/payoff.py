"""The distillation payoff, measured: the same held-out requests served before and after.

    uv run --group distill python -m distill.payoff banking77-intent

Before: the strong model served these exact requests in captured traffic (cost, latency and
answer from capture_events and the dataset). After: they are replayed through the gateway with
the task's rollout at its current state (run this with it at `full`) using a fresh simulated key
(so no cache hits), and every answer, served-by alias, cost and latency is recorded.

All numbers are from SIMULATED traffic (public Banking77 data replayed through the gateway).
Cost is list-price: tokens x models.yaml prices. The distilled model is self-hosted, so it has
no per-token price, and its compute is NOT priced here (no paid host exists to measure).
"""

import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from distill.evaluate import RESULTS, load_split
from distill.replay import _admin_token
from gateway.core.config import Settings
from gateway.db.models import CaptureEventRow
from gateway.db.session import make_engine
from gateway.distill.validators import normalize_label

SOURCE = "groq-gpt-oss-120b"


async def before_rows(settings: Settings, event_ids: list[str]) -> dict[str, CaptureEventRow]:
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            ids = [uuid.UUID(i) for i in event_ids]
            result = await conn.execute(select(CaptureEventRow).where(CaptureEventRow.id.in_(ids)))
            return {str(r.id): r for r in result}  # type: ignore[misc]
    finally:
        await engine.dispose()


def make_key(gateway: str) -> str:
    response = httpx.post(
        f"{gateway}/admin/keys",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "name": f"payoff run {time.strftime('%Y-%m-%d %H:%M')} (simulated)",
            "allowed_models": [SOURCE],
            "simulated": True,
            "distill_serving": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    return str(response.json()["key"])


def system_prompt(task: str) -> str:
    first = Path(f"data/tasks/{task}.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return str(json.loads(first)["messages"][0]["content"])


def pctile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def summarize(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r["gold"]]
    cost = sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))
    return {
        "label": label,
        "n": len(rows),
        "accuracy_vs_gold": round(sum(r["correct"] for r in scored) / len(scored), 4),
        "served_by_distilled": round(sum(r["distilled"] for r in rows) / len(rows), 4),
        "cost_per_1k_usd": format((cost / len(rows) * 1000).quantize(Decimal("0.000001")), "f"),
        "latency_ms_p50": round(statistics.median(r["latency_ms"] for r in rows), 1),
        "latency_ms_p95": round(pctile([r["latency_ms"] for r in rows], 0.95), 1),
    }


def main() -> None:
    task = sys.argv[1]
    gateway = os.environ.get("GATEWAY", "http://localhost:8000")
    rows, manifest = load_split(task, "test")
    before_db = asyncio.run(before_rows(Settings(), [r["source_event_id"] for r in rows]))
    before = []
    for r in rows:
        ev = before_db[r["source_event_id"]]
        before.append(
            {
                "gold": r.get("gold"),
                "correct": normalize_label(r["target"]) == normalize_label(r.get("gold") or ""),
                "distilled": False,
                "cost_usd": str(ev.cost_usd),
                "latency_ms": float(ev.latency_ms),
            }
        )

    key, system = make_key(gateway), system_prompt(task)
    after = []
    headers = {"Authorization": f"Bearer {key}", "X-Gateway-Simulated": "1"}
    with httpx.Client(base_url=gateway, headers=headers, timeout=120) as client:
        for i, r in enumerate(rows, 1):
            started = time.perf_counter()
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": SOURCE,
                    "temperature": 0,
                    "max_tokens": 512,
                    "reasoning_effort": "low",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": r["input"]},
                    ],
                },
            )
            latency = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"] or ""
            after.append(
                {
                    "gold": r.get("gold"),
                    "correct": normalize_label(answer) == normalize_label(r.get("gold") or ""),
                    "distilled": response.headers.get("x-gateway-alias", "").startswith(
                        "distilled"
                    ),
                    "distill_header": response.headers.get("x-gateway-distill"),
                    "cost_usd": response.headers.get("x-gateway-cost-usd", "0"),
                    "latency_ms": latency,
                }
            )
            if i % 25 == 0:
                print(f"  {i}/{len(rows)}", flush=True)

    result = {
        "task": task,
        "dataset_hash": manifest["data_hash"],
        "simulated": True,
        "before": summarize(f"strong only ({SOURCE})", before),
        "after": summarize("with the distilled rollout", after),
        "fallbacks": sum(1 for a in after if (a["distill_header"] or "").endswith("fallback")),
        "note": "List-price cost; the distilled model's self-hosted compute is not priced.",
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"payoff_{task}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
