"""Tests for odibi_anchor.codebase.convention_preflight_context."""

import pytest
from pathlib import Path

from odibi_anchor.codebase.convention_preflight_context import (
    convention_preflight_context,
    render_convention_preflight_report,
    _build_checklist,
    _build_examples,
    _scan_existing_patterns,
    _extract_signature,
    _VALID_ACTIONS,
    _STANDARD_CONTRACT_KEYS,
)


# Standard contract keys for all context generators
_CONTRACT_KEYS = {"kind", "subject", "summary", "metrics", "findings", "risks", "samples", "suggested_next_actions"}


@pytest.fixture(scope="module")
def project_root():
    """Use the real project root for pattern scanning tests."""
    return Path(__file__).resolve().parent.parent.parent


class TestConventionPreflightContract:
    """Verify output contract on valid calls."""

    def test_minimal_call__has_all_contract_keys(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_function", function_name="test_fn")
        assert _CONTRACT_KEYS.issubset(ctx.keys())
        assert ctx["kind"] == "convention_preflight_context"

    def test_checklist_key_present(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_tool", function_name="anomaly_context")
        assert "checklist" in ctx
        assert isinstance(ctx["checklist"], list)
        assert len(ctx["checklist"]) > 0

    def test_examples_key_present(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_tool", function_name="anomaly_context")
        assert "examples" in ctx
        assert isinstance(ctx["examples"], dict)

    def test_subject_defaults_to_function_name(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_function", function_name="my_func")
        assert ctx["subject"] == "my_func"

    def test_subject_defaults_to_target_file_stem(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_function", target_file="src/pkg/my_module.py")
        assert ctx["subject"] == "my_module.py"

    def test_subject_defaults_to_action(self, project_root):
        ctx = convention_preflight_context(project_root, action="refactor")
        assert ctx["subject"] == "refactor"


class TestConventionPreflightValidation:
    """Parameter validation."""

    def test_invalid_action__raises_value_error(self, project_root):
        with pytest.raises(ValueError, match="action must be one of"):
            convention_preflight_context(project_root, action="invalid_action")

    def test_invalid_root__raises_value_error(self):
        with pytest.raises(ValueError, match="root must be an existing directory"):
            convention_preflight_context("/nonexistent/path/xyz", action="new_function")

    def test_invalid_output_format__raises_value_error(self, project_root):
        with pytest.raises(ValueError):
            convention_preflight_context(project_root, action="new_function", output_format="xml")

    @pytest.mark.parametrize("action", sorted(_VALID_ACTIONS))
    def test_all_valid_actions_accepted(self, project_root, action):
        ctx = convention_preflight_context(project_root, action=action)
        assert ctx["kind"] == "convention_preflight_context"


class TestBuildChecklist:
    """Tests for _build_checklist logic."""

    def test_new_tool__has_contract_keys_rule(self):
        checklist = _build_checklist("new_tool", "my_context", None, {})
        rules = [c["rule"] for c in checklist]
        assert any("standard contract keys" in r.lower() or "contract keys" in r.lower() for r in rules)

    def test_new_tool__has_render_function_rule(self):
        checklist = _build_checklist("new_tool", "anomaly_context", None, {})
        rules = [c["rule"] for c in checklist]
        assert any("render_anomaly_report" in r for r in rules)

    def test_new_tool__has_output_format_rule(self):
        checklist = _build_checklist("new_tool", "my_context", None, {})
        rules = [c["rule"] for c in checklist]
        assert any("output_format" in r for r in rules)

    def test_new_function__has_standalone_rule(self):
        checklist = _build_checklist("new_function", "helper", None, {})
        rules = [c["rule"] for c in checklist]
        assert any("standalone" in r.lower() for r in rules)

    def test_add_parameter__has_backward_compatible_rule(self):
        checklist = _build_checklist("add_parameter", None, None, {})
        rules = [c["rule"] for c in checklist]
        assert any("keyword-only" in r.lower() or "backward-compatible" in r.lower() for r in rules)

    def test_modify_function__has_contract_warning(self):
        checklist = _build_checklist("modify_function", None, None, {})
        rules = [c["rule"] for c in checklist]
        assert any("return dict keys" in r.lower() for r in rules)

    def test_refactor__has_test_pass_rule(self):
        checklist = _build_checklist("refactor", None, None, {})
        rules = [c["rule"] for c in checklist]
        assert any("tests pass" in r.lower() for r in rules)

    def test_universal_output_format_validation_rule(self):
        for action in _VALID_ACTIONS:
            checklist = _build_checklist(action, None, None, {})
            rules = [c["rule"] for c in checklist]
            assert any("validate output_format" in r.lower() for r in rules)

    def test_checklist_items_have_severity(self):
        checklist = _build_checklist("new_tool", "fn", None, {})
        for item in checklist:
            assert "severity" in item
            assert item["severity"] in {"blocker", "warning"}

    def test_checklist_items_have_category(self):
        checklist = _build_checklist("new_tool", "fn", None, {})
        for item in checklist:
            assert "category" in item


class TestBuildExamples:
    """Tests for _build_examples logic."""

    def test_new_tool__suggested_signature(self):
        examples = _build_examples("new_tool", "anomaly_context", {})
        assert "suggested_signature" in examples
        assert "anomaly_context" in examples["suggested_signature"]

    def test_new_tool__suggested_render(self):
        examples = _build_examples("new_tool", "anomaly_context", {})
        assert "suggested_render" in examples
        assert "render_anomaly_report" in examples["suggested_render"]

    def test_new_tool__contract_keys_listed(self):
        examples = _build_examples("new_tool", "my_context", {})
        assert examples["return_contract_keys"] == _STANDARD_CONTRACT_KEYS

    def test_existing_signatures_propagated(self):
        patterns = {"function_signatures": [{"name": "fn1", "signature": "def fn1(x)"}]}
        examples = _build_examples("new_function", None, patterns)
        assert "existing_signatures" in examples

    def test_no_tool_action__no_suggested_signature(self):
        examples = _build_examples("refactor", None, {})
        assert "suggested_signature" not in examples


class TestScanExistingPatterns:
    """Tests for _scan_existing_patterns on real project."""

    def test_scan__finds_modules(self, project_root):
        patterns = _scan_existing_patterns(project_root)
        assert patterns["module_count"] > 0

    def test_scan__detects_output_format(self, project_root):
        patterns = _scan_existing_patterns(project_root)
        assert patterns["has_output_format"] is True

    def test_scan__detects_render_functions(self, project_root):
        patterns = _scan_existing_patterns(project_root)
        assert patterns["has_render_functions"] is True
        assert len(patterns["render_function_names"]) > 0

    def test_scan_order_is_deterministic(self, project_root):
        assert _scan_existing_patterns(project_root) == _scan_existing_patterns(project_root)

    def test_scan_does_not_hide_patterns_after_fifty_modules(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for index in range(51):
            (src / f"module_{index:02d}.py").write_text("def helper():\n    return None\n", encoding="utf-8")
        (src / "z_render.py").write_text("def render_late_report(ctx):\n    return str(ctx)\n", encoding="utf-8")

        patterns = _scan_existing_patterns(tmp_path)

        assert patterns["module_count"] == 52
        assert patterns["has_render_functions"] is True
        assert patterns["render_function_names"] == ["render_late_report"]

    def test_scan__detects_test_dirs(self, project_root):
        patterns = _scan_existing_patterns(project_root)
        assert "tests" in patterns["test_dirs"]


class TestMetrics:
    """Metrics dict structure."""

    def test_metrics__checklist_item_count(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_tool", function_name="fn")
        assert ctx["metrics"]["checklist_item_count"] == len(ctx["checklist"])

    def test_metrics__action_stored(self, project_root):
        ctx = convention_preflight_context(project_root, action="refactor")
        assert ctx["metrics"]["action"] == "refactor"

    def test_metrics__has_target_file(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_function", target_file="a.py")
        assert ctx["metrics"]["has_target_file"] is True

    def test_metrics__no_target_file(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_function")
        assert ctx["metrics"]["has_target_file"] is False


class TestRenderConventionPreflightReport:
    """Render tests."""

    def test_render__returns_string(self, project_root):
        ctx = convention_preflight_context(project_root, action="new_tool", function_name="my_ctx")
        md = render_convention_preflight_report(ctx)
        assert isinstance(md, str)
        assert "Convention Preflight" in md

    def test_output_format_markdown(self, project_root):
        result = convention_preflight_context(project_root, action="new_tool", function_name="fn", output_format="markdown")
        assert isinstance(result, str)

    def test_render__missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_convention_preflight_report({"kind": "convention_preflight_context"})


class TestExtractSignature:
    """Tests for _extract_signature AST helper."""

    def test_simple_function(self):
        import ast
        code = "def my_func(a, b, c): pass"
        tree = ast.parse(code)
        node = tree.body[0]
        sig = _extract_signature(node)
        assert sig == "def my_func(a, b, c)"

    def test_kwonly_args(self):
        import ast
        code = "def my_func(a, *, b, c): pass"
        tree = ast.parse(code)
        node = tree.body[0]
        sig = _extract_signature(node)
        assert "b=..." in sig
        assert "c=..." in sig
