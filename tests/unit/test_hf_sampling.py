from typing import Any

import pytest

from distill.datasets import hf


def fake_pages(total: int) -> Any:
    """A dataset sorted by label: rows 0-99 are label 0, rows 100-199 label 1, ..."""

    def page(_client: Any, _d: str, _c: str, _s: str, offset: int) -> dict[str, Any]:
        rows = [
            {"row": {"i": i, "label": i // 100}} for i in range(offset, min(offset + 100, total))
        ]
        return {"num_rows_total": total, "rows": rows}

    return page


def test_small_sorted_datasets_are_sampled_globally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "_page", fake_pages(7700))  # 77 labels x 100, sorted
    rows = list(hf.sample_rows("d", "c", "s", n=400, seed=7))
    assert len(rows) == 400
    assert len({r["label"] for r in rows}) >= 70  # page sampling would cover only ~4 labels


def test_sampling_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "_page", fake_pages(1000))
    a = [r["i"] for r in hf.sample_rows("d", "c", "s", n=50, seed=7)]
    b = [r["i"] for r in hf.sample_rows("d", "c", "s", n=50, seed=7)]
    assert a == b
