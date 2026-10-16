"""Operate a distilled model's rollout through the admin API (every change is audited).

    python -m distill.rollout_cli register banking77-intent --alias distilled-banking77-intent-v1
    python -m distill.rollout_cli state banking77-intent shadow
    python -m distill.rollout_cli state banking77-intent canary --weight 0.05
    python -m distill.rollout_cli state banking77-intent full --weight 1
    python -m distill.rollout_cli status

Shadow mode never affects responses, so any registered model may run in shadow (that's how
live evidence is gathered). `canary` and `full` serve real traffic and are refused unless the
model's M9 eval passed (distill/results/eval_<task>_<version>.json).
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from distill.discover import OUT
from distill.evaluate import RESULTS


def _token() -> str:
    if token := os.environ.get("ADMIN_TOKEN"):
        return token
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("ADMIN_TOKEN must be set (env or .env)")


def _client(gateway: str) -> httpx.Client:
    return httpx.Client(
        base_url=gateway, headers={"Authorization": f"Bearer {_token()}"}, timeout=30
    )


def _task(name: str) -> dict[str, Any]:
    tasks = json.loads((OUT / "tasks.json").read_text(encoding="utf-8"))
    return next(t for t in tasks if t["task"] == name)


def gate_passed(name: str, version: str) -> bool:
    gate = RESULTS / f"eval_{name}_{version}.json"
    return gate.exists() and bool(json.loads(gate.read_text(encoding="utf-8"))["passed"])


def register(gateway: str, name: str, alias: str, version: str) -> None:
    task = _task(name)
    with _client(gateway) as client:
        response = client.post(
            "/admin/distill/tasks",
            json={
                "task": name,
                "system_fingerprint": task["system_fingerprint"],
                "source_alias": task["source_alias"],
                "distilled_alias": alias,
                "validator": task["validator"],
                "version": version,
            },
        )
        response.raise_for_status()
    print(f"registered {name} -> {alias} (state: off)")


def set_state(gateway: str, name: str, state: str, weight: float, reason: str) -> None:
    fp = _task(name)["system_fingerprint"]
    with _client(gateway) as client:
        if state in ("canary", "full"):
            current = next(
                t
                for t in client.get("/admin/distill/tasks").json()
                if t["system_fingerprint"] == fp
            )
            if not gate_passed(name, current["version"]):
                sys.exit(
                    f"refusing {state}: {name} {current['version']} has not passed the eval gate"
                )
        response = client.post(
            f"/admin/distill/tasks/{fp}/state",
            json={"state": state, "weight": weight, "reason": reason},
        )
        response.raise_for_status()
        print(response.json())


def status(gateway: str, as_json: bool = False) -> None:
    with _client(gateway) as client:
        if as_json:
            print(json.dumps({"tasks": client.get("/admin/distill/tasks").json()}))
            return
        print(json.dumps(client.get("/admin/distill/tasks").json(), indent=2))
        print(json.dumps(client.get("/admin/distill/audit?limit=10").json(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="distill.rollout_cli")
    parser.add_argument("--gateway", default="http://localhost:8000")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("task")
    r.add_argument("--alias", required=True)
    r.add_argument("--version", default="v1")
    s = sub.add_parser("state")
    s.add_argument("task")
    s.add_argument("state", choices=["off", "shadow", "canary", "full", "rolled_back"])
    s.add_argument("--weight", type=float, default=0.0)
    s.add_argument("--reason", default="cli")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true", help="machine-readable (for distill.pipeline)")
    args = parser.parse_args()
    if args.cmd == "register":
        register(args.gateway, args.task, args.alias, args.version)
    elif args.cmd == "state":
        set_state(args.gateway, args.task, args.state, args.weight, args.reason)
    else:
        status(args.gateway, args.json)


if __name__ == "__main__":
    main()
