"""Tests for cap_with_hint (_utils/contract.py)."""
from odibi_anchor._utils.contract import cap_with_hint


def test_no_cap_when_under_limit():
    items, hint = cap_with_hint([1, 2, 3], 5)
    assert items == [1, 2, 3]
    assert hint is None


def test_caps_and_emits_hint():
    items, hint = cap_with_hint(list(range(10)), 3, unit="rows", param="limit")
    assert items == [0, 1, 2]
    assert hint is not None
    assert "7 more rows" in hint
    assert "limit=" in hint


def test_zero_or_negative_limit_means_no_cap():
    items, hint = cap_with_hint([1, 2, 3], 0)
    assert items == [1, 2, 3] and hint is None


def test_exact_limit_no_hint():
    items, hint = cap_with_hint([1, 2, 3], 3)
    assert items == [1, 2, 3] and hint is None
