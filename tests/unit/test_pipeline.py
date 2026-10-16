"""The M11 state machine: ordering, resume, waiting and stopping. Steps are faked."""

from pathlib import Path

import pytest

import distill.pipeline as pl
from distill.pipeline import STEPS, Pipeline, State, Stop, Waiting


class FakePipeline(Pipeline):
    """Records which steps ran; a step can be told to wait or stop."""

    def __init__(self, state: State, wait_at: str = "", stop_at: str = "") -> None:
        super().__init__(state, run=lambda args: "")
        self.ran: list[str] = []
        self.wait_at, self.stop_at = wait_at, stop_at
        for step in STEPS:
            setattr(self, f"step_{step}", self._make(step))

    def _make(self, step: str):  # type: ignore[no-untyped-def]
        def fn() -> None:
            self.ran.append(step)
            if step == self.wait_at:
                raise Waiting(f"{step}: needs live traffic")
            if step == self.stop_at:
                raise Stop(f"{step}: gate failed")

        return fn


@pytest.fixture(autouse=True)
def tmp_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pl, "OUT", tmp_path)


def test_runs_every_step_in_order_and_completes() -> None:
    p = FakePipeline(State("t"))
    state = p.advance()
    assert p.ran == list(STEPS)
    assert state.status == "complete"


def test_waiting_persists_and_resume_skips_finished_steps() -> None:
    first = FakePipeline(State("t"), wait_at="shadow")
    state = first.advance()
    assert state.status == "waiting" and "live traffic" in state.note
    assert first.ran[-1] == "shadow" and "shadow" not in state.done

    resumed = FakePipeline(State.load("t"))  # shadow can finish now
    final = resumed.advance()
    assert resumed.ran[0] == "shadow"  # discover..gate were not re-run
    assert final.status == "complete"


def test_a_failed_gate_stops_before_any_traffic_is_served() -> None:
    p = FakePipeline(State("t"), stop_at="gate")
    state = p.advance()
    assert state.status == "stopped"
    assert "canary5" not in p.ran and "full" not in p.ran
    assert State.load("t").status == "stopped"


def test_next_version_counts_existing_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pl, "MODELS_DIR", tmp_path)
    assert pl.next_version("task") == "v1"
    for v in ("v1", "v2", "v10", "smoke"):
        (tmp_path / "task" / v).mkdir(parents=True)
    assert pl.next_version("task") == "v11"
