"""D-001: every valid apply_sql execution requires a quality check."""
import pytest

from odibi_anchor._dispatcher._enforcement import should_block_data_write_unchecked
from odibi_anchor._dispatcher._pre_dispatch import _check_data_write


def _t(action, error=None, passed=False):
    return {"action": action, "error": error, "passed": passed, "elapsed_ms": 1.0}


# ── Pure decision function ──

def test_block_when_no_quality():
    blocked, msg = should_block_data_write_unchecked([_t("task"), _t("apply_transform")])
    assert blocked and "Delta write requires" in msg


def test_pass_after_quality():
    assert not should_block_data_write_unchecked([_t("quality"), _t("task")])[0]


def test_pass_after_pre_merge():
    assert not should_block_data_write_unchecked([_t("pre_merge")])[0]


def test_pass_after_validate():
    assert not should_block_data_write_unchecked([_t("validate")])[0]


def test_pass_after_duplicate():
    assert not should_block_data_write_unchecked([_t("duplicate")])[0]


def test_workflow_credit_investigate():
    assert not should_block_data_write_unchecked([_t("investigate")])[0]


def test_workflow_credit_evolve():
    assert not should_block_data_write_unchecked([_t("evolve")])[0]


def test_failed_quality_does_not_count():
    assert should_block_data_write_unchecked([_t("quality", error="boom")])[0]


def test_lookback_resets_at_passing_gate():
    # quality ran, but a passing gate after it closes the window → must re-check
    timings = [_t("quality"), _t("gate", passed=True)]
    assert should_block_data_write_unchecked(timings)[0]


def test_quality_after_gate_satisfies():
    timings = [_t("gate", passed=True), _t("quality")]
    assert not should_block_data_write_unchecked(timings)[0]


def test_empty_timings_blocks():
    assert should_block_data_write_unchecked([])[0]


# ── Wiring (_check_data_write) ──

def test_wiring_blocks_table_write_without_quality():
    with pytest.raises(RuntimeError, match="Delta write requires"):
        _check_data_write("apply_sql", {"mode": "table"}, [_t("task")])


def test_wiring_blocks_explicit_view_without_quality():
    with pytest.raises(RuntimeError, match="Delta write requires"):
        _check_data_write("apply_sql", {"mode": "view"}, [_t("task")])


def test_wiring_ignores_non_apply_sql():
    # apply_transform is in-memory, not a Delta write
    _check_data_write("apply_transform", {}, [_t("task")])


def test_wiring_allows_table_write_after_quality():
    _check_data_write("apply_sql", {"mode": "table"}, [_t("quality")])


def test_wiring_blocks_default_view_without_quality():
    with pytest.raises(RuntimeError, match="Delta write requires"):
        _check_data_write("apply_sql", {}, [_t("task")])


def test_wiring_invalid_mode_skips_legacy_quality_gate():
    _check_data_write("apply_sql", {"mode": "invalid"}, [_t("task")])
