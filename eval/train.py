"""Train the router classifier and score the baselines on a held-out test split."""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from eval.config import SEED, TEST_FRACTION
from eval.dataset import Example
from eval.metrics import curve, threshold_for

# Per-key quality settings, as targets for the share of the cheap->strong quality gap recovered.
QUALITY_TARGETS = {"low": 0.5, "balanced": 0.75, "high": 0.9}
C_GRID = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0)


@dataclass
class Split:
    train: list[Example]
    test: list[Example]


def stratified_split(examples: Sequence[Example]) -> Split:
    rng = random.Random(SEED)
    train: list[Example] = []
    test: list[Example] = []
    for label in (0, 1):
        group = [e for e in examples if e.label == label]
        rng.shuffle(group)
        cut = round(len(group) * TEST_FRACTION)
        test += group[:cut]
        train += group[cut:]
    return Split(sorted(train, key=lambda e: e.id), sorted(test, key=lambda e: e.id))


def heuristic_score(example: Example) -> float:
    """Baseline: short prompts without code or math go cheap. Higher = more cheap-worthy."""
    return -math.log1p(len(example.prompt)) - 2.0 * example.has_code - 1.0 * example.has_math


@dataclass
class Trained:
    weights: list[float]
    bias: float
    c: float
    thresholds: dict[str, float]
    cv_log_loss: float


def train_classifier(train: Sequence[Example], vectors: dict[str, list[float]]) -> Trained:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    x = np.array([vectors[e.id] for e in train])
    y = np.array([e.label for e in train])
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)

    def oof(c: float) -> Any:
        model = LogisticRegression(C=c, max_iter=5000)
        return cross_val_predict(model, x, y, cv=folds, method="predict_proba")[:, 1]

    def log_loss(p: Any) -> float:
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))

    scored = [(log_loss(oof(c)), c) for c in C_GRID]
    loss, best_c = min(scored)
    # Thresholds come from out-of-fold predictions on TRAIN, never from the test split.
    oof_scores = [float(s) for s in oof(best_c)]
    labels = [e.label for e in train]
    points = curve(
        oof_scores, labels, [e.cheap_cost for e in train], [e.strong_cost for e in train]
    )
    cheap_quality = sum(labels) / len(labels)
    thresholds = {
        name: threshold_for(points, cheap_quality, target)
        for name, target in QUALITY_TARGETS.items()
    }
    final = LogisticRegression(C=best_c, max_iter=5000).fit(x, y)
    return Trained(
        weights=[float(w) for w in final.coef_[0]],
        bias=float(final.intercept_[0]),
        c=best_c,
        thresholds=thresholds,
        cv_log_loss=loss,
    )


def predict(trained: Trained, vector: Sequence[float]) -> float:
    z = trained.bias + sum(w * v for w, v in zip(trained.weights, vector, strict=True))
    return 1.0 / (1.0 + math.exp(-z))
