"""D-008: anchor("status") surfaces data tools used and the data-quality posture."""
import odibi_anchor._dispatcher._compliance as comp


def _reset(timings):
    comp._SESSION_TIMINGS.clear()
    comp._SESSION_TIMINGS.extend(timings)
    comp._SESSION_FILES_CHANGED.clear()
    comp._SESSION_FILES_CREATED.clear()


def _t(a, e=None):
    return {"action": a, "error": e, "elapsed_ms": 1.0}


def test_data_tools_used_listed():
    _reset([_t("profile_table"), _t("quality"), _t("map")])
    ctx = comp._status(".", {}, None, None, output_format="dict")
    used = ctx["metrics"]["data_tools_used"]
    assert "profile_table" in used and "quality" in used
    assert "map" not in used  # not a data action


def test_data_posture_finding_when_unchecked():
    _reset([_t("apply_transform")])
    ctx = comp._status(".", {}, None, None, output_format="dict")
    assert any(f.startswith("DATA:") for f in ctx["findings"])


def test_no_data_finding_when_clean():
    _reset([_t("apply_transform"), _t("quality")])
    ctx = comp._status(".", {}, None, None, output_format="dict")
    assert not any(f.startswith("DATA:") for f in ctx["findings"])


def test_data_tools_used_empty_for_code_session():
    _reset([_t("map"), _t("test")])
    ctx = comp._status(".", {}, None, None, output_format="dict")
    assert ctx["metrics"]["data_tools_used"] == []
