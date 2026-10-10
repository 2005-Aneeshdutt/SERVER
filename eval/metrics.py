"""Routing metrics (pure Python, so they're unit-tested in CI without numpy/sklearn).

A router assigns each prompt a score = how confident it is the cheap model is good enough;
"route cheap if score >= t". Sweeping t traces a curve:
  x = share of calls sent to the strong model,
  y = quality = share of prompts that got an answer at least as good as the strong model's
      (a prompt sent to the strong model counts as good by definition).

PGR (performance gap recovered, from RouteLLM) normalizes quality between always-cheap (0)
and always-strong (1). APGR = area under the PGR-vs-strong-share curve: random routing scores
0.5; a perfect router approaches 1. CPT(x) = strong share needed to recover x of the gap.
"""

import itertools
import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    strong_share: float
    quality: float
    cost: float  # total USD over the evaluated prompts
    threshold: float


def curve(
    scores: Sequence[float],
    labels: Sequence[int],
    cheap_cost: Sequence[float],
    strong_cost: Sequence[float],
) -> list[Point]:
    """One point per distinct threshold, from all-strong to all-cheap."""
    n = len(scores)
    order = sorted(range(n), key=lambda i: -scores[i])  # most cheap-worthy first
    # Start with everything on the strong model; move prompts to cheap one by one.
    good, cost = float(n), float(sum(strong_cost))
    points = [Point(1.0, 1.0, cost, float("inf"))]
    for rank, i in enumerate(order, start=1):
        good += labels[i] - 1
        cost += cheap_cost[i] - strong_cost[i]
        last_of_tie = rank == n or scores[order[rank]] != scores[i]
        if last_of_tie:
            points.append(Point((n - rank) / n, good / n, cost, scores[i]))
    return sorted(points, key=lambda p: (p.strong_share, p.quality))


def pgr(quality: float, cheap_quality: float) -> float:
    gap = 1.0 - cheap_quality
    return (quality - cheap_quality) / gap if gap > 0 else 1.0


def _envelope(points: Sequence[Point]) -> list[tuple[float, float]]:
    """Best quality achievable without exceeding each strong share (monotone)."""
    best, out = float("-inf"), []
    for p in sorted(points, key=lambda p: p.strong_share):
        best = max(best, p.quality)
        out.append((p.strong_share, best))
    return out


def apgr(points: Sequence[Point], cheap_quality: float) -> float:
    env = _envelope(points)
    area = 0.0
    for (x0, y0), (x1, y1) in itertools.pairwise(env):
        area += (x1 - x0) * (pgr(y0, cheap_quality) + pgr(y1, cheap_quality)) / 2
    return area


def cpt(points: Sequence[Point], cheap_quality: float, target_pgr: float) -> float:
    """Smallest strong-call share reaching the target PGR."""
    for share, quality in _envelope(points):
        if pgr(quality, cheap_quality) >= target_pgr:
            return share
    return 1.0


def threshold_for(points: Sequence[Point], cheap_quality: float, target_pgr: float) -> float:
    """Lowest threshold (= most traffic to cheap) whose quality reaches the target PGR."""
    feasible = [p for p in points if pgr(p.quality, cheap_quality) >= target_pgr]
    best = min(feasible, key=lambda p: p.strong_share)
    return best.threshold


def bootstrap_apgr_diff(
    a: Sequence[float],
    b: Sequence[float],
    labels: Sequence[int],
    cheap_cost: Sequence[float],
    strong_cost: Sequence[float],
    rounds: int = 1000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """APGR(a) - APGR(b) with a 95% percentile bootstrap interval over test prompts."""
    rng = random.Random(seed)
    n, diffs = len(labels), []
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        sub_labels = [labels[i] for i in idx]
        q_cheap = sum(sub_labels) / n
        cc, sc = [cheap_cost[i] for i in idx], [strong_cost[i] for i in idx]
        da = apgr(curve([a[i] for i in idx], sub_labels, cc, sc), q_cheap)
        db = apgr(curve([b[i] for i in idx], sub_labels, cc, sc), q_cheap)
        diffs.append(da - db)
    diffs.sort()
    q_cheap = sum(labels) / n
    point = apgr(curve(a, labels, cheap_cost, strong_cost), q_cheap) - apgr(
        curve(b, labels, cheap_cost, strong_cost), q_cheap
    )
    return point, diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds) - 1]
