"""Tests for output cost-disclosure (_utils/_output_hints.py, AXI #9)."""
from odibi_anchor._utils._output_hints import (
    estimate_tokens,
    build_cost_hint,
    apply_output_cost_hint,
)


def test_estimate_tokens_string_and_dict():
    assert estimate_tokens("a" * 40) == 10
    assert estimate_tokens({"k": "v"}) >= 1


def test_no_hint_below_threshold():
    assert build_cost_hint("map", 100, already_toon=False, already_summary=False, threshold=8000) is None


def test_hint_suggests_toon_and_detail_for_map():
    h = build_cost_hint("map", 50000, already_toon=False, already_summary=False, threshold=8000)
    assert h is not None
    assert "detail='summary'" in h
    assert "output_format='toon'" in h
    assert "anchor('map'" in h


def test_hint_skips_toon_when_already_toon():
    h = build_cost_hint("map", 50000, already_toon=True, already_summary=False, threshold=8000)
    assert "output_format='toon'" not in h
    assert "detail='summary'" in h


def test_hint_skips_detail_when_already_summary():
    h = build_cost_hint("map", 50000, already_toon=False, already_summary=True, threshold=8000)
    assert "detail='summary'" not in h
    assert "output_format='toon'" in h


def test_non_detail_action_only_gets_toon():
    h = build_cost_hint("memory", 50000, already_toon=False, already_summary=False, threshold=8000)
    assert "detail='summary'" not in h
    assert "output_format='toon'" in h


def test_no_hint_when_all_knobs_exhausted():
    # already toon AND a non-detail-capable action → nothing left to suggest
    assert build_cost_hint("memory", 50000, already_toon=True, already_summary=False, threshold=8000) is None


def test_threshold_zero_disables():
    assert build_cost_hint("map", 10**9, already_toon=False, already_summary=False, threshold=0) is None


def test_apply_appends_to_dict_suggestions_and_records_estimate():
    out = {"metrics": {}, "suggested_next_actions": [], "big": "x" * 40000}
    res = apply_output_cost_hint("map", out, already_toon=False, threshold=8000)
    assert any("TIP: large output" in s for s in res["suggested_next_actions"])
    assert res["metrics"]["output_tokens_estimate"] >= 8000


def test_apply_appends_to_markdown_string():
    md = "# Codebase Map\n" + ("x" * 40000)
    res = apply_output_cost_hint("map", md, already_toon=False, threshold=8000)
    assert isinstance(res, str)
    assert "TIP: large output" in res


def test_apply_small_output_unchanged():
    out = {"metrics": {}, "suggested_next_actions": ["existing"]}
    res = apply_output_cost_hint("map", out, already_toon=False, threshold=8000)
    assert res["suggested_next_actions"] == ["existing"]


def test_apply_is_idempotent_on_strings():
    md = "# x\n" + ("y" * 40000)
    once = apply_output_cost_hint("map", md, already_toon=False, threshold=8000)
    twice = apply_output_cost_hint("map", once, already_toon=False, threshold=8000)
    assert twice.count("TIP: large output") == 1


def test_apply_does_not_fabricate_keys_on_plain_dict():
    out = {"some": "value"}  # not a contract dict
    res = apply_output_cost_hint("save", out, already_toon=False, threshold=8000)
    assert "suggested_next_actions" not in res
