"""H-005: error_trace_context routes skill hints by error category."""
from odibi_anchor.debugging.error_trace_context import error_trace_context


def _hints(text):
    ctx = error_trace_context(text, output_format="dict")
    return " ".join(ctx["suggested_next_actions"])


def test_import_error_routes_to_dependency_skill():
    tb = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'foo'"
    assert "dependency-management" in _hints(tb)


def test_generic_debugging_hints_still_present():
    tb = "Traceback (most recent call last):\nValueError: something"
    assert "skills/debugging/SKILL.md" in _hints(tb)
