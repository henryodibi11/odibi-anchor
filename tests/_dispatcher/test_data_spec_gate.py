"""Data-Spec persistence needs proven evidence without inventing Spec policy."""

from odibi_anchor._dispatcher._enforcement import should_block_spec_persist_evidence
from odibi_anchor.planning._task_builders import _DATA_SPEC_MODES

_DM = frozenset({"data", "etl", "reconciliation"})


def _t(a, e=None):
    return {"action": a, "error": e}


def test_data_spec_modes_constant():
    assert _DATA_SPEC_MODES == _DM
def test_blocks_data_persist_without_profile():
    assert should_block_spec_persist_evidence("data", [_t("task")], _DM)[0]


def test_allows_after_profile_table():
    assert not should_block_spec_persist_evidence("data", [_t("profile_table")], _DM)[0]


def test_allows_after_investigate():
    assert not should_block_spec_persist_evidence("etl", [_t("investigate")], _DM)[0]


def test_allows_after_contract():
    assert not should_block_spec_persist_evidence("reconciliation", [_t("contract")], _DM)[0]


def test_non_data_mode_not_evidence_gated():
    assert not should_block_spec_persist_evidence("implementation", [_t("task")], _DM)[0]


def test_failed_profile_does_not_count():
    assert should_block_spec_persist_evidence("data", [_t("profile_table", e="x")], _DM)[0]
