"""H-001: should_block_gate_preflight — preflight required for .py changes."""
from odibi_anchor._dispatcher._enforcement import should_block_gate_preflight


def _t(action, error=None, passed=None):
    d = {"action": action, "elapsed_ms": 1.0, "error": error}
    if passed is not None:
        d["passed"] = passed
    return d


def test_no_py_changes_never_blocks():
    blocked, _ = should_block_gate_preflight([_t("touched")], {"notes.md"})
    assert blocked is False


def test_py_changed_without_preflight_blocks():
    timings = [_t("task"), _t("touched")]
    blocked, msg = should_block_gate_preflight(timings, {"a.py"})
    assert blocked is True
    assert "Preflight required" in msg


def test_py_changed_with_preflight_passes():
    timings = [_t("task"), _t("touched"), _t("preflight")]
    blocked, _ = should_block_gate_preflight(timings, {"a.py"})
    assert blocked is False


def test_preflight_before_last_gate_does_not_count():
    # preflight ran, then a gate passed, then a NEW .py edit with no new preflight
    timings = [_t("preflight"), _t("gate", passed=True), _t("touched")]
    blocked, _ = should_block_gate_preflight(timings, {"a.py"})
    assert blocked is True


def test_failed_preflight_does_not_count():
    timings = [_t("preflight", error="boom")]
    blocked, _ = should_block_gate_preflight(timings, {"a.py"})
    assert blocked is True
