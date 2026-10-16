"""M11: the whole loop, one command, resumable.

    uv run --group distill --group train --extra router python -m distill.pipeline \
        run banking77-intent
    ... --resume            continue a pipeline that stopped waiting (training, shadow, canary)
    ... --gpu-handoff       don't train on this CPU: print the Kaggle/Colab command and stop

Steps (each records its outputs in data/distill/<task>/pipeline.json, so a re-run skips what's
done and picks up where it stopped):

    discover -> build -> train -> convert -> serve -> gate -> shadow -> canary 5% -> canary 25%
             -> full  (or stop at any failed gate; or rollback, which the gateway does by itself)

- build:   a new dataset only matters if its hash differs from the last trained model's.
- train:   on this CPU (train/train_lora.py), or a GPU handoff that waits for the model files.
- convert: GGUF (Q8_0) + a grammar for constrained decoding (distill/grammar.py).
- serve:   points llama.cpp at the new model (data/distill/SERVING) and redeploys.
- gate:    distill/evaluate.py, non-inferiority with a fixed margin. FAIL stops the pipeline
           before any real traffic is served (shadow still runs: it never affects responses).
- shadow:  waits for live traffic: >= `--shadow-min` comparisons and agreement >= the gate's
           measured agreement minus 5 points. Otherwise it stops and says why (resume later).
- canary:  5%, then 25%, each needing `--canary-min` online samples with no rollback, then full.
           The gateway rolls back by itself (validity + sampled audit, ADR 0016); the pipeline
           only promotes, and never re-promotes a rolled-back version.

Every state change goes through the admin API, so it is written to the audit trail.
Nightly: run with `--resume` from cron / Windows Task Scheduler; it is idempotent.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from distill.discover import OUT
from distill.evaluate import RESULTS

MODELS_DIR = Path(os.environ.get("DISTILL_MODELS_DIR", Path.home() / "distill-models"))
SERVING = OUT / "SERVING"
STEPS = (
    "discover",
    "build",
    "train",
    "convert",
    "serve",
    "gate",
    "shadow",
    "canary5",
    "canary25",
    "full",
)
GATEWAY = os.environ.get("GATEWAY", "http://localhost:8000")


class Waiting(Exception):
    """A step can't finish yet (needs live traffic or a GPU run): stop now, resume later."""


class Stop(Exception):
    """A step decided the pipeline must not go further (e.g. the eval gate failed)."""


@dataclass
class State:
    task: str
    done: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    note: str = ""

    @classmethod
    def load(cls, task: str) -> "State":
        path = OUT / task / "pipeline.json"
        if path.exists():
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        return cls(task)

    def save(self) -> None:
        path = OUT / self.task / "pipeline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


Runner = Callable[[Sequence[str]], str]


def shell(args: Sequence[str]) -> str:
    print("  $", " ".join(args), flush=True)
    result = subprocess.run(list(args), check=True, capture_output=True, text=True)
    return result.stdout


@dataclass
class Pipeline:
    state: State
    run: Runner = shell
    gpu_handoff: bool = False
    shadow_min: int = 50
    canary_min: int = 100

    # ---- orchestration ----------------------------------------------------------------------

    def advance(self) -> State:
        s = self.state
        try:
            for step in STEPS:
                if step in s.done:
                    continue
                print(f"[{s.task}] {step}", flush=True)
                getattr(self, f"step_{step}")()
                s.done.append(step)
                s.save()
            s.status, s.note = "complete", "full rollout"
        except Waiting as exc:
            s.status, s.note = "waiting", str(exc)
        except Stop as exc:
            s.status, s.note = "stopped", str(exc)
        s.save()
        print(f"[{s.task}] {s.status}: {s.note}", flush=True)
        return s

    def _tools(self, *module_args: str) -> str:
        return self.run(
            ["docker", "compose", "--profile", "distill", "run", "--rm", "distill-tools",
             "python", "-m", *module_args]
        )  # fmt: skip

    # ---- steps ------------------------------------------------------------------------------

    def step_discover(self) -> None:
        self._tools("distill.discover")
        tasks = json.loads((OUT / "tasks.json").read_text(encoding="utf-8"))
        if not any(t["task"] == self.state.task and t.get("score", 0) > 0 for t in tasks):
            raise Stop(f"{self.state.task} is not a distillation candidate (no validator/volume)")

    def step_build(self) -> None:
        self._tools("distill.build", self.state.task)
        latest = (OUT / self.state.task / "LATEST").read_text(encoding="utf-8").strip()
        manifest = json.loads((OUT / self.state.task / latest / "manifest.json").read_text("utf-8"))
        self.state.data["dataset_hash"] = manifest["data_hash"]
        self.state.data["version"] = next_version(self.state.task)

    def _model_dir(self) -> Path:
        return MODELS_DIR / self.state.task / str(self.state.data["version"])

    def step_train(self) -> None:
        out = self._model_dir()
        if (out / "hf" / "config.json").exists() and (out / "train_config.json").exists():
            trained = json.loads((out / "train_config.json").read_text(encoding="utf-8"))
            if trained["dataset_hash"] != self.state.data["dataset_hash"]:
                raise Stop(f"{out} was trained on a different dataset; remove it and resume")
            return
        command = [
            "uv", "run", "--group", "train", "--extra", "router", "python", "train/train_lora.py",
            "--data", str(OUT / self.state.task), "--out", str(out),
            "--epochs", "4", "--top-layers", "1.0",
        ]  # fmt: skip
        if self.gpu_handoff:
            raise Waiting(
                "train on a GPU (Kaggle/Colab), then copy the output into "
                f"{out} and run again with --resume:\n    " + " ".join(command)
            )
        self.run(command)

    def step_convert(self) -> None:
        from distill.grammar import gbnf

        out = self._model_dir()
        if not (out / "model.gguf").exists():
            self.run(["bash", "scripts/to_gguf.sh", str(out)])
        (out / "grammar.gbnf").write_text(gbnf(self._manifest()["validator"]), encoding="utf-8")

    def _manifest(self) -> dict[str, Any]:
        latest = (OUT / self.state.task / "LATEST").read_text(encoding="utf-8").strip()
        manifest: dict[str, Any] = json.loads(
            (OUT / self.state.task / latest / "manifest.json").read_text(encoding="utf-8")
        )
        return manifest

    def step_serve(self) -> None:
        SERVING.write_text(f"{self.state.task}/{self.state.data['version']}\n", encoding="utf-8")
        self.run(["bash", "scripts/distill-up.sh"])

    def step_gate(self) -> None:
        version = str(self.state.data["version"])
        self.run(["uv", "run", "python", "-m", "distill.evaluate", self.state.task,
                  "--version", version])  # fmt: skip
        result = json.loads(
            (RESULTS / f"eval_{self.state.task}_{version}.json").read_text(encoding="utf-8")
        )
        self.state.data["gate_agreement"] = result["agreement_with_strong"]
        self._cli("register", self.state.task, "--alias", self._alias(), "--version", version)
        self._cli("state", self.state.task, "shadow", "--reason", f"pipeline: {version} in shadow")
        if not result["passed"]:
            raise Stop(
                f"eval gate FAILED for {version}: diff {result['diff'] * 100:+.1f} pts, "
                f"95% CI [{result['ci95'][0] * 100:+.1f}, {result['ci95'][1] * 100:+.1f}], "
                f"margin {result['margin'] * 100:.0f}. Left in shadow (serves no one)."
            )

    def _alias(self) -> str:
        from gateway.core.config import Settings
        from gateway.routing.registry import ModelRegistry

        registry = ModelRegistry.from_yaml(Settings().models_file)
        aliases = [s.alias for s in registry.all() if s.distilled_task == self.state.task]
        if not aliases:
            raise Stop(f"no distilled alias for {self.state.task} in models.yaml")
        return aliases[0]

    def _cli(self, *args: str) -> None:
        self.run(["uv", "run", "python", "-m", "distill.rollout_cli", *args])

    def _task_stats(self) -> dict[str, Any]:
        out = self.run(["uv", "run", "python", "-m", "distill.rollout_cli", "status", "--json"])
        tasks: list[dict[str, Any]] = json.loads(out)["tasks"]
        return next(t for t in tasks if t["task"] == self.state.task)

    def step_shadow(self) -> None:
        t = self._task_stats()
        if t["state"] == "rolled_back":
            raise Stop("rolled back during shadow")
        needed = float(self.state.data.get("gate_agreement", 0.9)) - 0.05
        if t["shadow_n"] < self.shadow_min:
            raise Waiting(f"shadow: {t['shadow_n']}/{self.shadow_min} comparisons so far")
        agreement = t["shadow_agree"] / t["shadow_n"]
        if agreement < needed:
            raise Stop(f"shadow agreement {agreement:.1%} < required {needed:.1%}")

    def _canary(self, weight: float, label: str) -> None:
        t = self._task_stats()
        if t["state"] == "rolled_back":
            raise Stop(f"rolled back automatically during {label}; see the audit trail")
        if t["state"] == "shadow" or t["weight"] < weight:
            self._cli("state", self.state.task, "canary", "--weight", str(weight),
                      "--reason", f"pipeline: {label}")  # fmt: skip
            self.state.data[f"{label}_started"] = time.time()
            raise Waiting(f"{label}: started; needs {self.canary_min} online samples")
        if t["window_n"] < self.canary_min:
            raise Waiting(f"{label}: {t['window_n']}/{self.canary_min} online samples")

    def step_canary5(self) -> None:
        self._canary(0.05, "canary5")

    def step_canary25(self) -> None:
        self._canary(0.25, "canary25")

    def step_full(self) -> None:
        t = self._task_stats()
        if t["state"] == "rolled_back":
            raise Stop("rolled back automatically before full; see the audit trail")
        self._cli("state", self.state.task, "full", "--weight", "1", "--reason", "pipeline: full")


def next_version(task: str) -> str:
    existing = (
        [int(p.name[1:]) for p in (MODELS_DIR / task).glob("v*") if p.name[1:].isdigit()]
        if (MODELS_DIR / task).exists()
        else []
    )
    return f"v{max(existing, default=0) + 1}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="distill.pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--resume", action="store_true", help="continue from pipeline.json")
    r.add_argument("--gpu-handoff", action="store_true")
    r.add_argument("--shadow-min", type=int, default=50)
    r.add_argument("--canary-min", type=int, default=100)
    s = sub.add_parser("status")
    s.add_argument("task")
    args = parser.parse_args()
    if args.cmd == "status":
        print(json.dumps(State.load(args.task).__dict__, indent=2))
        return
    state = State.load(args.task) if args.resume else State(args.task)
    if state.status == "stopped" and args.resume:
        sys.exit(f"pipeline stopped: {state.note}. Start a new run (without --resume).")
    state.status = "running"
    pipeline = Pipeline(
        state, gpu_handoff=args.gpu_handoff, shadow_min=args.shadow_min,
        canary_min=args.canary_min,
    )  # fmt: skip
    final = pipeline.advance()
    sys.exit(0 if final.status in ("complete", "waiting") else 1)


if __name__ == "__main__":
    main()
