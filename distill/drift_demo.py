"""M12: drift demo. Shows the gateway detecting drift and rolling a distilled model back.

    uv run --group distill python -m distill.drift_demo banking77-intent [--per-phase 60]

Precondition: the task's distilled model is in `canary` or `full` (ideally through the
pipeline, after passing the gate), and the gateway runs with a high audit rate for the demo
(`DISTILL_AUDIT_RATE=1.0 bash scripts/distill-up.sh`) so detection takes tens of requests,
not hundreds. All traffic is simulated (a simulated key, public Banking77 data).

Phase A, in distribution: held-out examples of the intents the model was trained on.
Phase B, drift: real Banking77 messages for intents the model has *never* seen (new product
features showing up in production). The model still answers with a valid label from its own
set (grammar-constrained), so the validity check stays silent; the sampled audit against the
strong model is what has to catch it.

Output: distill/results/drift_<task>.json (per-request timeline + stats snapshots + audit
events) and distill/results/drift_<task>.svg (a chart), both rendered in distill/RESULTS.md.
"""

import argparse
import json
import random
import time
from html import escape
from typing import Any

import httpx

from distill.datasets import TASKS
from distill.discover import OUT
from distill.evaluate import RESULTS
from distill.payoff import make_key, system_prompt
from distill.rollout_cli import _client

SOURCE = "groq-gpt-oss-120b"


def phases(task: str, per_phase: int, seed: int) -> tuple[list[str], list[str]]:
    """(in-distribution texts, drifted texts), neither used in training."""
    build = OUT / task / (OUT / task / "LATEST").read_text(encoding="utf-8").strip()
    manifest = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    known_labels = set(manifest["validator"]["labels"])
    trained = {
        json.loads(line)["input"]
        for line in (build / "train.jsonl").read_text("utf-8").splitlines()
    }
    test = [json.loads(line) for line in (build / "test.jsonl").read_text("utf-8").splitlines()]
    in_dist = [r["input"] for r in test]
    pool = [json.loads(line) for line in (TASKS / f"{task}.jsonl").read_text("utf-8").splitlines()]
    drifted = [
        e["messages"][-1]["content"]
        for e in pool
        if e["gold"] not in known_labels and e["messages"][-1]["content"] not in trained
    ]
    rng = random.Random(seed)
    rng.shuffle(in_dist)
    rng.shuffle(drifted)
    return in_dist[:per_phase], drifted[:per_phase]


def snapshot(admin: httpx.Client, task: str) -> dict[str, Any]:
    t = next(x for x in admin.get("/admin/distill/tasks").json() if x["task"] == task)
    return {
        k: t[k]
        for k in ("state", "weight", "window_n", "window_failures", "audit_n", "audit_disagree")
    }


def run(task: str, gateway: str, per_phase: int, seed: int, pause_s: float) -> dict[str, Any]:
    in_dist, drifted = phases(task, per_phase, seed)
    key, system = make_key(gateway), system_prompt(task)
    timeline: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {key}", "X-Gateway-Simulated": "1"}
    with (
        httpx.Client(base_url=gateway, headers=headers, timeout=120) as client,
        _client(gateway) as admin,
    ):
        start = snapshot(admin, task)
        if start["state"] not in ("canary", "full"):
            raise SystemExit(f"{task} is {start['state']}: put it in canary/full first")
        for phase, texts in (("in_distribution", in_dist), ("drift", drifted)):
            for text in texts:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": SOURCE,
                        "temperature": 0,
                        "max_tokens": 512,
                        "reasoning_effort": "low",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": text},
                        ],
                    },
                )
                time.sleep(pause_s)  # let the background audit for this request land
                timeline.append(
                    {
                        "i": len(timeline),
                        "phase": phase,
                        "status": response.status_code,
                        "served": response.headers.get("x-gateway-alias"),
                        "distill": response.headers.get("x-gateway-distill"),
                        **snapshot(admin, task),
                    }
                )
        audit = admin.get("/admin/distill/audit?limit=20").json()
    return {"task": task, "simulated": True, "per_phase": per_phase, "start": start,
            "timeline": timeline, "audit": audit}  # fmt: skip


INK, MUTED, GRID, RED, BLUE = "#18181b", "#71717a", "#e4e4e7", "#dc2626", "#4f46e5"


def _path(points: list[tuple[float, float]]) -> str:
    return " ".join(
        f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}" for i, (px, py) in enumerate(points)
    )


def _vline(x: float, top: int, bottom: int, color: str, label: str, label_y: int) -> list[str]:
    return [
        f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{bottom}" stroke="{color}" '
        'stroke-width="2" stroke-dasharray="4 4"/>',
        f'<text x="{x + 4:.1f}" y="{label_y}" font-size="11" fill="{color}">{label}</text>',
    ]


def chart(result: dict[str, Any]) -> str:
    """Audit disagreement rate and share served by the distilled model, per request."""
    rows = result["timeline"]
    w, h, left, top, bottom = 900, 320, 56, 40, 50
    plot_w, plot_h = w - left - 20, h - top - bottom
    n = max(1, len(rows) - 1)

    def x(i: int) -> float:
        return left + plot_w * i / n

    def y(v: float) -> float:
        return top + plot_h * (1 - v)

    disagree = [
        (x(r["i"]), y(r["audit_disagree"] / r["audit_n"] if r["audit_n"] else 0)) for r in rows
    ]
    served: list[tuple[float, float]] = []
    distilled = 0
    for k, r in enumerate(rows, 1):
        distilled += (r["served"] or "").startswith("distilled")
        served.append((x(r["i"]), y(distilled / k)))
    drift_start = next((r["i"] for r in rows if r["phase"] == "drift"), None)
    rollback = next((r["i"] for r in rows if r["state"] == "rolled_back"), None)
    title = f"{escape(result['task'])}: drift detection by sampled audit (simulated traffic)"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        'font-family="system-ui, sans-serif">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="{left}" y="22" font-size="15" font-weight="600" fill="{INK}">{title}</text>',
    ]
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(
            f'<line x1="{left}" x2="{w - 20}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{GRID}"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y(v) + 4:.1f}" font-size="11" fill="{MUTED}" '
            f'text-anchor="end">{v:.0%}</text>'
        )
    if drift_start is not None:
        parts += _vline(x(drift_start), top, h - bottom, MUTED, "drift starts", top + 12)
    if rollback is not None:
        parts += _vline(x(rollback), top, h - bottom, RED, "auto rollback", top + 26)
    parts.append(f'<path d="{_path(disagree)}" fill="none" stroke="{RED}" stroke-width="2"/>')
    parts.append(f'<path d="{_path(served)}" fill="none" stroke="{BLUE}" stroke-width="2"/>')
    parts.append(
        f'<text x="{left}" y="{h - 12}" font-size="11" fill="{BLUE}">'
        "— share served by the distilled model</text>"
    )
    parts.append(
        f'<text x="{left + 260}" y="{h - 12}" font-size="11" fill="{RED}">'
        "— audit disagreement with the strong model (10-min window)</text>"
    )
    parts.append(
        f'<text x="{w - 20}" y="{h - 30}" font-size="11" fill="{MUTED}" '
        'text-anchor="end">request #</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(prog="distill.drift_demo")
    parser.add_argument("task")
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--per-phase", type=int, default=60)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--pause", type=float, default=1.5)
    args = parser.parse_args()
    result = run(args.task, args.gateway, args.per_phase, args.seed, args.pause)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"drift_{args.task}.json").write_text(json.dumps(result, indent=2), "utf-8")
    (RESULTS / f"drift_{args.task}.svg").write_text(chart(result), encoding="utf-8")
    last = result["timeline"][-1]
    print(
        f"final state: {last['state']}; audit {last['audit_disagree']}/{last['audit_n']} disagreed"
    )


if __name__ == "__main__":
    main()
