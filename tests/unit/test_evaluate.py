from distill.evaluate import evaluate, paired_bootstrap

SPEC = {"type": "label_set", "labels": ["a", "b"]}


def rows(golds: list[str], strong: list[str]) -> list[dict[str, str]]:
    return [
        {"example_id": str(i), "gold": g, "target": s}
        for i, (g, s) in enumerate(zip(golds, strong, strict=True))
    ]


def test_scores_both_models_against_gold() -> None:
    r = rows(["a", "a", "b", "b"], ["a", "b", "b", "b"])  # strong: 3/4
    result = evaluate(r, ["a", "a", "b", "x"], SPEC)  # distilled: 3/4, one invalid
    assert result["strong_accuracy"] == 0.75 and result["distilled_accuracy"] == 0.75
    assert result["distilled_valid"] == 0.75
    assert result["diffs"] == [0, 1, 0, -1]


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean() -> None:
    diffs = [0] * 80 + [1] * 10 + [-1] * 10  # mean 0
    lo, hi = paired_bootstrap(diffs, 2000, seed=7)
    assert (lo, hi) == paired_bootstrap(diffs, 2000, seed=7)
    assert lo < 0 < hi and hi - lo < 0.2


def test_clearly_worse_model_has_ci_below_zero() -> None:
    diffs = [0] * 60 + [-1] * 40
    _, hi = paired_bootstrap(diffs, 2000, seed=7)
    assert hi < 0  # would fail any non-inferiority margin smaller than ~0.3
