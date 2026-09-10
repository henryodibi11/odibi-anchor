"""D-003: gate-time soft warning when data is transformed but never quality-checked."""
from odibi_anchor._dispatcher._enforcement import data_quality_posture


def _t(a, e=None):
    return {"action": a, "error": e}


def test_clean_when_no_mutation():
    assert data_quality_posture([_t("map"), _t("test")]) == ""


def test_warns_on_unchecked_apply_transform():
    assert "no quality" in data_quality_posture([_t("apply_transform")])


def test_warns_on_unchecked_apply_sql():
    assert "no quality" in data_quality_posture([_t("apply_sql")])


def test_warns_on_unchecked_coerce_fix():
    assert "no quality" in data_quality_posture([_t("coerce_fix")])


def test_quiet_when_checked_with_quality():
    assert data_quality_posture([_t("apply_transform"), _t("quality")]) == ""


def test_quiet_when_checked_with_workflow():
    assert data_quality_posture([_t("apply_transform"), _t("investigate")]) == ""


def test_failed_transform_is_not_a_mutation():
    assert data_quality_posture([_t("apply_transform", e="boom")]) == ""


def test_failed_quality_does_not_clear_posture():
    assert "no quality" in data_quality_posture(
        [_t("apply_transform"), _t("quality", e="boom")]
    )
