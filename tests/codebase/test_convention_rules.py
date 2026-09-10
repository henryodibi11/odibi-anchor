"""Tests for _convention_rules.py — Phase 1 anti-pattern rules."""

import pytest

from odibi_anchor.codebase._convention_rules import (
    ConventionViolation,
    run_anti_pattern_rules,
)


# ---------------------------------------------------------------------------
# Rule 1: no_hardcoded_paths
# ---------------------------------------------------------------------------


class TestNoHardcodedPaths:
    """Tests for the no_hardcoded_paths rule."""

    def test_detects_workspace_path(self, tmp_path):
        """Databricks /Workspace/Users/ path is flagged."""
        (tmp_path / "bad.py").write_text(
            'sys.path.insert(0, "/Workspace/Users/someone@example.com/lib")\n'
        )
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        assert len(violations) == 1
        assert violations[0].rule == "no_hardcoded_paths"
        assert violations[0].line == 1
        assert violations[0].severity == "warning"

    def test_detects_windows_forward_slash_path(self, tmp_path):
        """Windows C:/Users/ path is flagged."""
        (tmp_path / "bad.py").write_text(
            'data = open("C:/Users/someone/data.csv")\n'
        )
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        assert len(violations) == 1
        assert violations[0].rule == "no_hardcoded_paths"

    def test_ignores_comment_lines(self, tmp_path):
        """Paths in comments are not flagged."""
        (tmp_path / "ok.py").write_text(
            "# See /Workspace/Users/someone@example.com/readme\nx = 1\n"
        )
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        assert len(violations) == 0

    def test_clean_file_no_violations(self, tmp_path):
        """Normal file with no paths produces no violations."""
        (tmp_path / "clean.py").write_text("x = 1\ny = x + 2\n")
        violations = run_anti_pattern_rules(["clean.py"], str(tmp_path))
        assert len(violations) == 0

    def test_skips_non_python_files(self, tmp_path):
        """Non-.py files are skipped entirely."""
        (tmp_path / "readme.md").write_text("/Workspace/Users/x/path\n")
        violations = run_anti_pattern_rules(["readme.md"], str(tmp_path))
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Rule 2: no_deep_ternaries
# ---------------------------------------------------------------------------


class TestNoDeepTernaries:
    """Tests for the no_deep_ternaries rule."""

    def test_detects_nested_ternary(self, tmp_path):
        """Ternary inside ternary is flagged."""
        code = "x = a if cond1 else (b if cond2 else c)\n"
        (tmp_path / "nested.py").write_text(code)
        violations = run_anti_pattern_rules(["nested.py"], str(tmp_path))
        assert len(violations) == 1
        assert violations[0].rule == "no_deep_ternaries"
        assert violations[0].line == 1
        assert violations[0].severity == "warning"

    def test_detects_triple_nested_ternary(self, tmp_path):
        """Three levels of nesting still produces at least one violation."""
        code = "x = a if c1 else (b if c2 else (d if c3 else e))\n"
        (tmp_path / "deep.py").write_text(code)
        violations = run_anti_pattern_rules(["deep.py"], str(tmp_path))
        assert len(violations) >= 1
        assert all(v.rule == "no_deep_ternaries" for v in violations)

    def test_allows_single_ternary(self, tmp_path):
        """A simple (non-nested) ternary is fine."""
        code = "x = a if condition else b\n"
        (tmp_path / "simple.py").write_text(code)
        violations = run_anti_pattern_rules(["simple.py"], str(tmp_path))
        assert len(violations) == 0

    def test_allows_multiple_independent_ternaries(self, tmp_path):
        """Multiple ternaries on separate lines are fine."""
        code = "x = a if c1 else b\ny = d if c2 else e\n"
        (tmp_path / "multi.py").write_text(code)
        violations = run_anti_pattern_rules(["multi.py"], str(tmp_path))
        assert len(violations) == 0

    def test_skips_syntax_errors(self, tmp_path):
        """Files with syntax errors are skipped gracefully."""
        (tmp_path / "broken.py").write_text("def f(:\n")
        violations = run_anti_pattern_rules(["broken.py"], str(tmp_path))
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestOrchestrator:
    """Tests for run_anti_pattern_rules orchestrator."""

    def test_empty_file_list(self, tmp_path):
        """Empty changed_files returns no violations."""
        violations = run_anti_pattern_rules([], str(tmp_path))
        assert violations == []

    def test_missing_files_skipped(self, tmp_path):
        """Non-existent files are skipped without error."""
        violations = run_anti_pattern_rules(["does_not_exist.py"], str(tmp_path))
        assert violations == []

    def test_multiple_rules_fire(self, tmp_path):
        """Both rules can fire on the same file."""
        code = (
            'path = "/Workspace/Users/x/lib"\n'
            "x = a if c1 else (b if c2 else c)\n"
        )
        (tmp_path / "multi_issues.py").write_text(code)
        violations = run_anti_pattern_rules(["multi_issues.py"], str(tmp_path))
        rules_found = {v.rule for v in violations}
        assert "no_hardcoded_paths" in rules_found
        assert "no_deep_ternaries" in rules_found

    def test_violation_dataclass_fields(self, tmp_path):
        """ConventionViolation has all expected fields."""
        (tmp_path / "bad.py").write_text(
            'x = "/Workspace/Users/test@test.com/foo"\n'
        )
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        v = violations[0]
        assert v.rule == "no_hardcoded_paths"
        assert v.file == "bad.py"
        assert v.line == 1
        assert isinstance(v.message, str)
        assert v.severity in ("warning", "blocker")
        assert isinstance(v.fix_hint, str)


# ---------------------------------------------------------------------------
# Rule 3: boundary_message_match
# ---------------------------------------------------------------------------


class TestBoundaryMessageMatch:
    """Tests for the boundary_message_match rule."""

    def test_detects_gte_with_maximum(self, tmp_path):
        """'>= N' with 'Maximum: N' is flagged as off-by-one."""
        code = (
            "if count >= 4:\n"
            "    raise RuntimeError(\"Maximum: 4\")\n"
        )
        (tmp_path / "bad.py").write_text(code)
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        boundary = [v for v in violations if v.rule == "boundary_message_match"]
        assert len(boundary) == 1
        assert ">= 4" in boundary[0].message
        assert boundary[0].severity == "warning"

    def test_allows_gt_with_maximum(self, tmp_path):
        """'> N' with 'Maximum: N' is correct — no violation."""
        code = (
            "if count > 4:\n"
            "    raise RuntimeError(\"Maximum: 4\")\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        boundary = [v for v in violations if v.rule == "boundary_message_match"]
        assert len(boundary) == 0

    def test_detects_lte_with_minimum(self, tmp_path):
        """'<= N' with 'Minimum: N' is flagged as off-by-one."""
        code = (
            "if size <= 2:\n"
            "    raise ValueError(\"Minimum: 2\")\n"
        )
        (tmp_path / "bad.py").write_text(code)
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        boundary = [v for v in violations if v.rule == "boundary_message_match"]
        assert len(boundary) == 1

    def test_allows_lt_with_minimum(self, tmp_path):
        """'< N' with 'Minimum: N' is correct — no violation."""
        code = (
            "if size < 2:\n"
            "    raise ValueError(\"Minimum: 2\")\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        boundary = [v for v in violations if v.rule == "boundary_message_match"]
        assert len(boundary) == 0

    def test_no_raise_no_violation(self, tmp_path):
        """If-block without raise is not checked."""
        code = (
            "if x >= 4:\n"
            "    print(\"hello\")\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        boundary = [v for v in violations if v.rule == "boundary_message_match"]
        assert len(boundary) == 0


# ---------------------------------------------------------------------------
# Rule 4: no_unbound_variables
# ---------------------------------------------------------------------------


class TestNoUnboundVariables:
    """Tests for the no_unbound_variables rule."""

    def test_detects_unbound_in_if_only(self, tmp_path):
        """Variable assigned only in if-branch but used after is flagged."""
        code = (
            "def process(x):\n"
            "    if x > 0:\n"
            "        msg = \"positive\"\n"
            "    print(msg)\n"
        )
        (tmp_path / "bad.py").write_text(code)
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        unbound = [v for v in violations if v.rule == "no_unbound_variables"]
        assert len(unbound) == 1
        assert "msg" in unbound[0].message
        assert unbound[0].severity == "warning"

    def test_allows_pre_initialized(self, tmp_path):
        """Variable initialized before the if-block is fine."""
        code = (
            "def process(x):\n"
            "    msg = \"default\"\n"
            "    if x > 0:\n"
            "        msg = \"positive\"\n"
            "    print(msg)\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        unbound = [v for v in violations if v.rule == "no_unbound_variables"]
        assert len(unbound) == 0

    def test_allows_if_else_assignment(self, tmp_path):
        """Variable assigned in both if and else is safe."""
        code = (
            "def process(x):\n"
            "    if x > 0:\n"
            "        msg = \"pos\"\n"
            "    else:\n"
            "        msg = \"neg\"\n"
            "    print(msg)\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        unbound = [v for v in violations if v.rule == "no_unbound_variables"]
        assert len(unbound) == 0

    def test_allows_parameter_names(self, tmp_path):
        """Function parameters are not flagged."""
        code = (
            "def process(msg):\n"
            "    if True:\n"
            "        msg = \"override\"\n"
            "    print(msg)\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        unbound = [v for v in violations if v.rule == "no_unbound_variables"]
        assert len(unbound) == 0

    def test_no_flag_if_not_used_after(self, tmp_path):
        """Variable in if-only but never used after is fine."""
        code = (
            "def process(x):\n"
            "    if x > 0:\n"
            "        tmp = x * 2\n"
            "    return x\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        unbound = [v for v in violations if v.rule == "no_unbound_variables"]
        assert len(unbound) == 0


# ---------------------------------------------------------------------------
# Rule 5: no_dead_branches
# ---------------------------------------------------------------------------


class TestNoDeadBranches:
    """Tests for the no_dead_branches rule."""

    def test_detects_code_after_return(self, tmp_path):
        """Statements after return are unreachable."""
        code = "def f():\n    return 1\n    x = 2\n"
        (tmp_path / "dead.py").write_text(code)
        violations = run_anti_pattern_rules(["dead.py"], str(tmp_path))
        dead = [v for v in violations if v.rule == "no_dead_branches"]
        assert len(dead) == 1
        assert dead[0].line == 3
        assert dead[0].severity == "warning"

    def test_detects_code_after_raise(self, tmp_path):
        """Statements after raise are unreachable."""
        code = "def f():\n    raise ValueError('x')\n    cleanup()\n"
        (tmp_path / "dead.py").write_text(code)
        violations = run_anti_pattern_rules(["dead.py"], str(tmp_path))
        dead = [v for v in violations if v.rule == "no_dead_branches"]
        assert len(dead) == 1

    def test_detects_code_after_continue(self, tmp_path):
        """Statements after continue in a loop body are unreachable."""
        code = "def f():\n    for x in items:\n        if x > 0:\n            continue\n            print(x)\n"
        (tmp_path / "dead.py").write_text(code)
        violations = run_anti_pattern_rules(["dead.py"], str(tmp_path))
        dead = [v for v in violations if v.rule == "no_dead_branches"]
        assert len(dead) == 1

    def test_allows_code_before_return(self, tmp_path):
        """Code before return is fine."""
        code = "def f():\n    x = 2\n    return x\n"
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        dead = [v for v in violations if v.rule == "no_dead_branches"]
        assert len(dead) == 0

    def test_allows_return_at_end(self, tmp_path):
        """Return as last statement has no dead code."""
        code = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        dead = [v for v in violations if v.rule == "no_dead_branches"]
        assert len(dead) == 0


# ---------------------------------------------------------------------------
# Rule 6: list_or_single_documented
# ---------------------------------------------------------------------------


class TestListOrSingleDocumented:
    """Tests for the list_or_single_documented rule."""

    def test_detects_str_param_in_for_loop(self, tmp_path):
        """str-typed param iterated in for-loop is flagged."""
        code = "def process(target: str):\n    for item in target:\n        print(item)\n"
        (tmp_path / "bad.py").write_text(code)
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        lv = [v for v in violations if v.rule == "list_or_single_documented"]
        assert len(lv) == 1
        assert "target" in lv[0].message

    def test_detects_str_param_in_extend(self, tmp_path):
        """str-typed param passed to .extend() is flagged."""
        code = "def build(path: str):\n    cmd = []\n    cmd.extend(path)\n"
        (tmp_path / "bad.py").write_text(code)
        violations = run_anti_pattern_rules(["bad.py"], str(tmp_path))
        lv = [v for v in violations if v.rule == "list_or_single_documented"]
        assert len(lv) == 1

    def test_allows_isinstance_guarded(self, tmp_path):
        """str param with isinstance guard is not flagged."""
        code = (
            "def process(target: str):\n"
            "    if isinstance(target, list):\n"
            "        for item in target:\n"
            "            print(item)\n"
        )
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        lv = [v for v in violations if v.rule == "list_or_single_documented"]
        assert len(lv) == 0

    def test_allows_non_str_param(self, tmp_path):
        """Non-str typed param iterated is fine."""
        code = "def process(items: list):\n    for item in items:\n        print(item)\n"
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        lv = [v for v in violations if v.rule == "list_or_single_documented"]
        assert len(lv) == 0

    def test_allows_unannotated_param(self, tmp_path):
        """Param without type annotation is not flagged."""
        code = "def process(target):\n    for item in target:\n        print(item)\n"
        (tmp_path / "ok.py").write_text(code)
        violations = run_anti_pattern_rules(["ok.py"], str(tmp_path))
        lv = [v for v in violations if v.rule == "list_or_single_documented"]
        assert len(lv) == 0
