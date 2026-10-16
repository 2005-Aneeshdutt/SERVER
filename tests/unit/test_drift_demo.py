from distill.drift_demo import chart


def row(i: int, phase: str, served: str, state: str, n: int, d: int) -> dict[str, object]:
    return {
        "i": i,
        "phase": phase,
        "status": 200,
        "served": served,
        "distill": None,
        "state": state,
        "weight": 1.0,
        "window_n": 0,
        "window_failures": 0,
        "audit_n": n,
        "audit_disagree": d,
    }


def test_chart_marks_drift_and_rollback() -> None:
    timeline = [row(0, "in_distribution", "distilled-x", "full", 1, 0)]
    timeline += [row(1, "drift", "distilled-x", "full", 2, 1)]
    timeline += [row(2, "drift", "groq-gpt-oss-120b", "rolled_back", 3, 2)]
    svg = chart({"task": "t", "timeline": timeline})
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "drift starts" in svg and "auto rollback" in svg


def test_chart_without_rollback_has_no_rollback_marker() -> None:
    timeline = [row(i, "in_distribution", "distilled-x", "full", i + 1, 0) for i in range(3)]
    assert "auto rollback" not in chart({"task": "t", "timeline": timeline})
