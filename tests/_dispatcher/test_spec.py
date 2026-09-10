"""Tests for odibi_anchor._dispatcher._spec — spec lifecycle and criteria checking."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os

from odibi_anchor._dispatcher._spec import (
    check_spec_criteria,
    render_spec_criteria,
    _match_criterion,
    spec_action,
    _STATUS_EMOJI,
    _SCAFFOLD_TEMPLATE,
)


class TestMatchCriterion:
    """Tests for _match_criterion heuristic matching."""

    def test_file_exists__found(self, tmp_path):
        (tmp_path / "src" / "module.py").parent.mkdir(parents=True)
        (tmp_path / "src" / "module.py").write_text("# code")
        emoji, check_type, evidence = _match_criterion(
            "File src/module.py must exist", [], tmp_path
        )
        assert emoji == "✅"
        assert check_type == "file_exists"

    def test_file_exists__not_found(self, tmp_path):
        emoji, check_type, evidence = _match_criterion(
            "File src/missing.py must exist", [], tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "file_missing"

    def test_test_pass__test_ran_and_passed(self):
        timings = [{"action": "test", "error": None}]
        emoji, check_type, evidence = _match_criterion(
            "All tests pass", timings, Path("/fake")
        )
        assert emoji == "✅"
        assert check_type == "test_passed"

    def test_test_pass__no_test_run(self):
        emoji, check_type, evidence = _match_criterion(
            "Tests must pass", [], Path("/fake")
        )
        assert emoji == "⬜"
        assert check_type == "test_not_run"

    def test_test_pass__test_ran_with_error(self):
        timings = [{"action": "test", "error": "some error"}]
        emoji, check_type, evidence = _match_criterion(
            "All tests passing", timings, Path("/fake")
        )
        assert emoji == "⬜"
        assert check_type == "test_not_run"

    def test_cw_action__action_ran(self):
        timings = [{"action": "validate", "error": None}]
        emoji, check_type, evidence = _match_criterion(
            "Must run anchor(\'validate\')", timings, Path("/fake")
        )
        assert emoji == "✅"
        assert check_type == "action_ran"

    def test_cw_action__action_not_run(self):
        emoji, check_type, evidence = _match_criterion(
            "Must run anchor(\'validate\')", [], Path("/fake")
        )
        assert emoji == "⬜"
        assert check_type == "action_not_run"

    def test_default__agent_attested(self):
        emoji, check_type, evidence = _match_criterion(
            "Code review completed by team lead", [], Path("/fake")
        )
        assert emoji == "⬜"
        assert check_type == "agent_attested"


class TestCheckSpecCriteria:
    """Tests for check_spec_criteria."""

    def test_basic_output_structure(self, tmp_path):
        spec = {"name": "MY_SPEC", "success_criteria": ["Tests must pass"]}
        result = check_spec_criteria(spec, [], str(tmp_path))
        assert result["spec_name"] == "MY_SPEC"
        assert "criteria" in result
        assert len(result["criteria"]) == 1

    def test_criteria_entry_structure(self, tmp_path):
        spec = {"name": "X", "success_criteria": ["File x.py exists"]}
        result = check_spec_criteria(spec, [], str(tmp_path))
        entry = result["criteria"][0]
        assert "criterion" in entry
        assert "status" in entry
        assert "check_type" in entry
        assert "evidence" in entry

    def test_multiple_criteria(self, tmp_path):
        spec = {"name": "X", "success_criteria": ["a", "b", "c"]}
        result = check_spec_criteria(spec, [], str(tmp_path))
        assert len(result["criteria"]) == 3

    def test_empty_criteria(self, tmp_path):
        spec = {"name": "X", "success_criteria": []}
        result = check_spec_criteria(spec, [], str(tmp_path))
        assert result["criteria"] == []


class TestRenderSpecCriteria:
    """Tests for render_spec_criteria."""

    def test_renders_markdown_string(self):
        spec_criteria = {
            "spec_name": "MY_SPEC",
            "criteria": [
                {"criterion": "Tests pass", "status": "✅", "check_type": "test_passed", "evidence": "ok"},
            ],
        }
        result = render_spec_criteria(spec_criteria)
        assert isinstance(result, str)
        assert "MY_SPEC" in result
        assert "Tests pass" in result

    def test_empty_criteria__returns_empty(self):
        result = render_spec_criteria({"spec_name": "X", "criteria": []})
        assert result == ""


class TestSpecAction:
    """Tests for spec_action sub-commands."""

    def test_list__no_specs(self, tmp_path):
        result = spec_action(str(tmp_path), "list", specs_dir=str(tmp_path))
        # Should handle gracefully when no specs exist
        assert "No specs found" in str(result) or (isinstance(result, dict) and result.get("total", 0) == 0)

    def test_create__scaffolds_file(self, tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        result = spec_action(str(tmp_path), "create", name="MY_NEW_SPEC", specs_dir=str(specs_dir))
        # Should create a file
        created_files = list(specs_dir.glob("*.md"))
        assert len(created_files) == 1
        content = created_files[0].read_text()
        assert "My New Spec" in content or "MY_NEW_SPEC" in content or "Specification" in content
        assert "status: draft" in content

    def test_from_problem_creates_and_backlinks_spec(self, tmp_path):
        from odibi_anchor._dispatcher._problem import problem_action

        created = problem_action(
            tmp_path,
            "create",
            title="Reduce queue latency",
            recommendation="Increase worker capacity, then measure p95 latency.",
            evidence={"source": "load test 42", "observation": "CPU exceeded 95%."},
            recommendation_evidence="E1",
            uncertainty="The load test covered one traffic pattern.",
            output_format="dict",
        )

        result = spec_action(
            str(tmp_path),
            "from_problem",
            created["problem_id"],
            name="QUEUE_CAPACITY",
            specs_dir=tmp_path / "specs",
            output_format="dict",
        )

        content = Path(result["path"]).read_text(encoding="utf-8")
        problem = problem_action(tmp_path, "show", created["problem_id"], output_format="dict")
        assert result["problem_id"] == created["problem_id"]
        assert f"problem_id: {created['problem_id']}" in content
        assert "recommendation_revision:" in content
        assert "evidence_ids: [E1]" in content
        assert "`E1` — load test 42" in content
        assert "load test 42" in content
        assert problem["linked_specs"] == ["QUEUE_CAPACITY"]

    def test_from_problem_requires_recommendation(self, tmp_path):
        from odibi_anchor._dispatcher._problem import problem_action

        created = problem_action(
            tmp_path,
            "create",
            title="Unresolved queue latency",
            output_format="dict",
        )
        with pytest.raises(ValueError, match="captured recommendation"):
            spec_action(
                str(tmp_path),
                "from_problem",
                created["problem_id"],
                specs_dir=tmp_path / "specs",
                output_format="dict",
            )

    def test_from_problem_rejects_non_current_recommendation_revision(self, tmp_path):
        from odibi_anchor._dispatcher._problem import problem_action

        created = problem_action(
            tmp_path,
            "create",
            title="Queue latency",
            evidence={"source": "load test", "observation": "CPU exceeded 95%."},
            recommendation="Scale workers.",
            recommendation_evidence="E1",
            uncertainty="Traffic mix may change.",
            output_format="dict",
        )

        with pytest.raises(ValueError, match="current integer revision"):
            spec_action(
                str(tmp_path),
                "from_problem",
                created["problem_id"],
                recommendation_revision=999,
                specs_dir=tmp_path / "specs",
                output_format="dict",
            )
        with pytest.raises(ValueError, match="current integer revision"):
            spec_action(
                str(tmp_path),
                "from_problem",
                created["problem_id"],
                recommendation_revision=True,
                specs_dir=tmp_path / "specs",
                output_format="dict",
            )

    def test_unknown_sub_command__returns_error(self, tmp_path):
        result = spec_action(str(tmp_path), "invalid_command", output_format="dict")
        assert "error" in result or "Unknown" in str(result)

    def test_status__spec_not_found(self, tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        result = spec_action(str(tmp_path), "status", "nonexistent", specs_dir=str(specs_dir), output_format="dict")
        assert "error" in result or "not found" in str(result).lower()


class TestStatusEmoji:
    """Verify _STATUS_EMOJI constants."""

    def test_covers_main_statuses(self):
        assert "done" in _STATUS_EMOJI
        assert "in-progress" in _STATUS_EMOJI
        assert "ready" in _STATUS_EMOJI
        assert "draft" in _STATUS_EMOJI

    def test_done_is_checkmark(self):
        assert _STATUS_EMOJI["done"] == "✅"


# ---------------------------------------------------------------------------
# Phase 2: _spec_persist and _spec_from_task tests
# ---------------------------------------------------------------------------

from odibi_anchor._dispatcher._spec import (
    _spec_from_task,
    _spec_persist,
    _sanitize_spec_name,
)


class TestSanitizeSpecName:
    """Tests for _sanitize_spec_name."""

    def test_basic_conversion(self):
        assert _sanitize_spec_name("Add persist sub-command") == "ADD_PERSIST_SUB_COMMAND"

    def test_strips_special_chars(self):
        assert _sanitize_spec_name("Hello! World? #1") == "HELLO_WORLD_1"

    def test_empty_returns_unnamed(self):
        assert _sanitize_spec_name("") == "UNNAMED_SPEC"

    def test_collapses_multiple_underscores(self):
        assert _sanitize_spec_name("test---multiple---dashes") == "TEST_MULTIPLE_DASHES"

    def test_whitespace_only(self):
        assert _sanitize_spec_name("   ") == "UNNAMED_SPEC"


class TestSpecFromTask:
    """Tests for _spec_from_task."""

    def _full_task_result(self):
        return {
            "goal": "Build the widget",
            "mode": "implementation",
            "readiness": {"score": 95},
            "acceptance_criteria": ["Widget renders", "Tests pass"],
            "in_scope": ["Widget component"],
            "out_of_scope": ["Backend API"],
            "constraints": ["No external deps"],
            "risks": ["May break on mobile"],
            "background": "Users need a new widget.",
            "deliverables": ["widget.py"],
            "action_plan": [{"name": "Design"}, {"name": "Implement"}, {"name": "Test"}],
            "artifacts": [{"path": "src/widget.py"}],
        }

    def test_produces_yaml_frontmatter(self):
        content = _spec_from_task(self._full_task_result())
        lines = content.split("\n")
        assert lines[0] == "---"
        # Find second ---
        end_idx = lines.index("---", 1)
        assert end_idx > 0

    def test_frontmatter_has_required_fields(self):
        content = _spec_from_task(self._full_task_result())
        assert "status: draft" in content
        assert "mode: implementation" in content
        assert "readiness_score: 95" in content
        assert "created:" in content
        assert "phases:" in content
        assert "success_criteria:" in content
        assert "files_touched:" in content
        assert "permissions_needed: []" in content

    def test_body_has_required_sections(self):
        content = _spec_from_task(self._full_task_result())
        assert "## Problem" in content
        assert "## Acceptance Criteria" in content
        assert "## Risks" in content
        assert "## Phases" in content
        assert "## Verification" in content

    def test_acceptance_criteria_are_checkboxes(self):
        content = _spec_from_task(self._full_task_result())
        assert "- [ ] Widget renders" in content
        assert "- [ ] Tests pass" in content

    def test_background_appears_in_problem_section(self):
        content = _spec_from_task(self._full_task_result())
        assert "Users need a new widget." in content

    def test_minimal_input_produces_valid_spec(self):
        content = _spec_from_task({"goal": "Fix it", "mode": "debugging"})
        assert "status: draft" in content
        assert "mode: debugging" in content
        assert "readiness_score: 0" in content
        assert "[Describe the problem]" in content

    def test_action_plan_generates_phases(self):
        content = _spec_from_task(self._full_task_result())
        assert "1. Design" in content
        assert "2. Implement" in content
        assert "3. Test" in content

    def test_scope_section_present(self):
        content = _spec_from_task(self._full_task_result())
        assert "**In scope:**" in content
        assert "- Widget component" in content
        assert "**Out of scope:**" in content
        assert "- Backend API" in content


class TestSpecPersist:
    """Tests for _spec_persist."""

    def test_creates_spec_file(self, tmp_path):
        task_result = {"goal": "Add feature", "mode": "implementation"}
        result = _spec_persist(tmp_path, task_result, "dict")
        assert result["created"] is True
        assert (tmp_path / "ADD_FEATURE_SPEC.md").exists()

    def test_file_content_has_frontmatter(self, tmp_path):
        task_result = {"goal": "Add feature", "mode": "implementation"}
        _spec_persist(tmp_path, task_result, "dict")
        content = (tmp_path / "ADD_FEATURE_SPEC.md").read_text()
        assert content.startswith("---\n")
        assert "status: draft" in content

    def test_duplicate_returns_error(self, tmp_path):
        task_result = {"goal": "Add feature", "mode": "implementation"}
        _spec_persist(tmp_path, task_result, "dict")
        result2 = _spec_persist(tmp_path, task_result, "dict")
        assert "error" in result2
        assert "already exists" in result2["error"]

    def test_none_input_returns_error(self, tmp_path):
        result = _spec_persist(tmp_path, None, "dict")
        assert "error" in result

    def test_empty_dict_returns_error(self, tmp_path):
        result = _spec_persist(tmp_path, {}, "dict")
        assert "error" in result

    def test_no_goal_returns_error(self, tmp_path):
        result = _spec_persist(tmp_path, {"mode": "testing"}, "dict")
        assert "error" in result

    def test_summary_field_used_when_goal_missing(self, tmp_path):
        task_result = {"summary": "Fix the bug", "mode": "debugging"}
        result = _spec_persist(tmp_path, task_result, "dict")
        assert result["created"] is True
        assert "FIX_THE_BUG" in result["name"]

    def test_markdown_output_format(self, tmp_path):
        task_result = {"goal": "Add feature", "mode": "implementation"}
        result = _spec_persist(tmp_path, task_result, "markdown")
        # Always returns dict now so post-dispatch can set spec_persisted=True
        assert result["created"] is True
        assert "Persisted spec" in result["summary"]

    def test_spec_appears_in_list(self, tmp_path):
        """Persisted spec should be findable by list_specs."""
        task_result = {"goal": "New widget", "mode": "implementation"}
        _spec_persist(tmp_path, task_result, "dict")
        result = spec_action("/tmp", "list", specs_dir=str(tmp_path), output_format="dict")
        assert result["total"] == 1
        assert result["specs"][0]["status"] == "draft"

    def test_dispatch_via_spec_action(self, tmp_path):
        """anchor('spec', 'persist', task_result) routes correctly."""
        task_result = {"goal": "Widget work", "mode": "testing"}
        result = spec_action("/tmp", "persist", task_result, specs_dir=str(tmp_path), output_format="dict")
        assert result["created"] is True
