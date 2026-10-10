import pytest

from eval.metrics import apgr, bootstrap_apgr_diff, cpt, curve, pgr, threshold_for

# 4 prompts: the cheap model is good enough on #0 and #1 only.
LABELS = [1, 1, 0, 0]
CHEAP = [1.0, 1.0, 1.0, 1.0]
STRONG = [10.0, 10.0, 10.0, 10.0]
PERFECT = [0.9, 0.8, 0.2, 0.1]  # ranks the good-enough prompts first
WORST = [0.1, 0.2, 0.8, 0.9]


def test_curve_endpoints_are_always_strong_and_always_cheap() -> None:
    points = curve(PERFECT, LABELS, CHEAP, STRONG)
    assert (points[-1].strong_share, points[-1].quality, points[-1].cost) == (1.0, 1.0, 40.0)
    assert (points[0].strong_share, points[0].quality, points[0].cost) == (0.0, 0.5, 4.0)


def test_perfect_router_recovers_the_whole_gap_at_half_the_calls() -> None:
    points = curve(PERFECT, LABELS, CHEAP, STRONG)
    halfway = next(p for p in points if p.strong_share == 0.5)
    assert halfway.quality == 1.0
    assert cpt(points, 0.5, 1.0) == 0.5
    assert apgr(points, 0.5) == pytest.approx(0.75)  # 0 -> 1 linearly over [0, .5], then 1


def test_worst_router_is_below_random() -> None:
    assert apgr(curve(WORST, LABELS, CHEAP, STRONG), 0.5) < 0.5


def test_tied_scores_move_together() -> None:
    points = curve([0.5, 0.5, 0.5, 0.5], LABELS, CHEAP, STRONG)
    assert [p.strong_share for p in points] == [0.0, 1.0]


def test_pgr_and_threshold_selection() -> None:
    assert pgr(0.75, 0.5) == 0.5
    points = curve(PERFECT, LABELS, CHEAP, STRONG)
    assert threshold_for(points, 0.5, 1.0) == 0.8  # route cheap iff score >= 0.8


def test_bootstrap_difference_has_the_right_sign() -> None:
    labels = LABELS * 10
    diff, low, high = bootstrap_apgr_diff(
        PERFECT * 10, WORST * 10, labels, CHEAP * 10, STRONG * 10, rounds=200
    )
    assert diff > 0 and low > 0 and high >= low
