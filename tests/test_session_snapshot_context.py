"""Tests for session_snapshot_context."""

import json
import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odibi_anchor.codebase import (
    session_snapshot_context,
    render_session_snapshot_report,
    save_snapshot,
    load_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_project(tmp_path):
    """Create a minimal project structure."""
    # Python files
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("# init\n")
    (src / "main.py").write_text("""def hello():\n    return 'world'\n\ndef add(a, b):\n    return a + b\n""")
    (src / "utils.py").write_text("import os\n\ndef helper():\n    pass\n")

    # Tests
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text("def test_hello():\n    pass\n\ndef test_add():\n    pass\n")

    # Docs
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Project\n\nA test project.\n")

    # Config
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\ntestpaths = [\"tests\"]\n")

    return tmp_path


# ---------------------------------------------------------------------------
# TestOutputContract
# ---------------------------------------------------------------------------

class TestOutputContract:
    """Verify standard context dict contract."""

    def test_kind(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        assert ctx["kind"] == "session_snapshot_context"

    def test_required_keys(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        required = {"kind", "subject", "summary", "metrics", "findings", "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_subject_defaults_to_dirname(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        assert ctx["subject"] == sample_project.name

    def test_subject_override(self, sample_project):
        ctx = session_snapshot_context(sample_project, subject="my_project")
        assert ctx["subject"] == "my_project"

    def test_timestamp_present(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        assert "timestamp" in ctx
        assert "T" in ctx["timestamp"]  # ISO format


# ---------------------------------------------------------------------------
# TestAutoScan
# ---------------------------------------------------------------------------

class TestAutoScan:
    """Verify automatic file scanning."""

    def test_finds_all_tracked_files(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        states = ctx["file_states"]
        # Should find: __init__.py, main.py, utils.py, test_main.py, README.md, pyproject.toml
        assert len(states) == 6

    def test_file_states_have_required_fields(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        for path, state in ctx["file_states"].items():
            assert "lines" in state
            assert "size" in state
            assert "hash" in state
            assert isinstance(state["lines"], int)
            assert isinstance(state["size"], int)
            assert isinstance(state["hash"], str)
            assert len(state["hash"]) == 12  # truncated sha256

    def test_ignores_hidden_dirs(self, sample_project):
        hidden = sample_project / ".git"
        hidden.mkdir()
        (hidden / "config").write_text("some config")
        ctx = session_snapshot_context(sample_project)
        assert not any(".git" in p for p in ctx["file_states"])

    def test_ignores_pycache(self, sample_project):
        cache = sample_project / "src" / "__pycache__"
        cache.mkdir()
        (cache / "main.cpython-311.pyc").write_text("bytecode")
        ctx = session_snapshot_context(sample_project)
        assert not any("__pycache__" in p for p in ctx["file_states"])

    def test_ignores_untracked_extensions(self, sample_project):
        (sample_project / "image.png").write_bytes(b"\x89PNG")
        (sample_project / "data.csv").write_text("a,b,c\n1,2,3")
        ctx = session_snapshot_context(sample_project)
        assert not any(p.endswith(".png") for p in ctx["file_states"])
        assert not any(p.endswith(".csv") for p in ctx["file_states"])

    def test_metrics_accuracy(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        m = ctx["metrics"]
        assert m["file_count"] == 6
        assert m["py_file_count"] == 4  # __init__.py, main.py, utils.py, test_main.py
        assert m["md_file_count"] == 1  # README.md
        assert m["total_lines"] > 0
        assert m["total_size_bytes"] > 0


# ---------------------------------------------------------------------------
# TestReasoningInputs
# ---------------------------------------------------------------------------

class TestReasoningInputs:
    """Verify that manual reasoning inputs are stored correctly."""

    def test_decisions_stored(self, sample_project):
        decisions = ["Used AST for zero deps", "Rejected class pattern"]
        ctx = session_snapshot_context(sample_project, decisions=decisions)
        assert ctx["decisions"] == decisions

    def test_rejected_alternatives(self, sample_project):
        rejected = ["tree-sitter: too heavy"]
        ctx = session_snapshot_context(sample_project, rejected_alternatives=rejected)
        assert ctx["rejected_alternatives"] == rejected

    def test_open_questions(self, sample_project):
        questions = ["Should we add output_format?"]
        ctx = session_snapshot_context(sample_project, open_questions=questions)
        assert ctx["open_questions"] == questions
        assert len(ctx["risks"]) > 0  # open questions generate a risk

    def test_next_steps(self, sample_project):
        steps = ["Write tests", "Update docs"]
        ctx = session_snapshot_context(sample_project, next_steps=steps)
        assert ctx["next_steps"] == steps
        assert ctx["suggested_next_actions"][:len(steps)] == steps

    def test_session_notes(self, sample_project):
        ctx = session_snapshot_context(sample_project, session_notes="All going well.")
        assert ctx["session_notes"] == "All going well."

    def test_defaults_to_empty_lists(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        assert ctx["decisions"] == []
        assert ctx["rejected_alternatives"] == []
        assert ctx["open_questions"] == []
        assert ctx["next_steps"] == []
        assert ctx["session_notes"] is None

    def test_test_state(self, sample_project):
        ctx = session_snapshot_context(sample_project, test_state={"passed": 50, "failed": 2, "skipped": 0})
        assert ctx["test_state"]["passed"] == 50
        assert ctx["test_state"]["failed"] == 2
        # Failing tests should generate a risk
        assert any("failing" in r.lower() or "fail" in r.lower() for r in ctx["risks"])


# ---------------------------------------------------------------------------
# TestSnapshotDiffing
# ---------------------------------------------------------------------------

class TestSnapshotDiffing:
    """Verify diffing between snapshots."""

    def test_no_previous_means_no_changes(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        assert ctx["changes_since"] is None

    def test_detects_added_files(self, sample_project):
        # Take initial snapshot
        prev = session_snapshot_context(sample_project)
        # Add a file
        (sample_project / "new_file.py").write_text("# new\n")
        # Take new snapshot with previous
        ctx = session_snapshot_context(sample_project, previous_snapshot=prev)
        assert "new_file.py" in ctx["changes_since"]["added"]

    def test_detects_modified_files(self, sample_project):
        prev = session_snapshot_context(sample_project)
        # Modify a file
        (sample_project / "src" / "main.py").write_text("# completely different content\n")
        ctx = session_snapshot_context(sample_project, previous_snapshot=prev)
        assert "src/main.py" in ctx["changes_since"]["modified"]

    def test_detects_removed_files(self, sample_project):
        prev = session_snapshot_context(sample_project)
        # Remove a file
        (sample_project / "src" / "utils.py").unlink()
        ctx = session_snapshot_context(sample_project, previous_snapshot=prev)
        assert "src/utils.py" in ctx["changes_since"]["removed"]

    def test_unchanged_count(self, sample_project):
        prev = session_snapshot_context(sample_project)
        # Change one file
        (sample_project / "src" / "main.py").write_text("changed\n")
        ctx = session_snapshot_context(sample_project, previous_snapshot=prev)
        changes = ctx["changes_since"]
        assert changes["unchanged_count"] == 5  # 6 total - 1 modified

    def test_summary_mentions_changes(self, sample_project):
        prev = session_snapshot_context(sample_project)
        (sample_project / "new.py").write_text("x\n")
        ctx = session_snapshot_context(sample_project, previous_snapshot=prev)
        assert "changed since last snapshot" in ctx["summary"]


# ---------------------------------------------------------------------------
# TestSaveLoad
# ---------------------------------------------------------------------------

class TestSaveLoad:
    """Verify persistence helpers."""

    def test_save_creates_file(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        save_path = sample_project / ".session_snapshot.json"
        save_snapshot(ctx, save_path)
        assert save_path.exists()

    def test_load_returns_snapshot(self, sample_project):
        ctx = session_snapshot_context(sample_project, decisions=["test decision"])
        save_path = sample_project / ".session_snapshot.json"
        save_snapshot(ctx, save_path)
        loaded = load_snapshot(save_path)
        assert loaded["kind"] == "session_snapshot_context"
        assert loaded["decisions"] == ["test decision"]

    def test_load_nonexistent_returns_none(self, tmp_path):
        result = load_snapshot(tmp_path / "does_not_exist.json")
        assert result is None

    def test_roundtrip_preserves_data(self, sample_project):
        ctx = session_snapshot_context(
            sample_project,
            decisions=["decision A"],
            next_steps=["step 1", "step 2"],
            test_state={"passed": 10, "failed": 0, "skipped": 1},
        )
        save_path = sample_project / "snapshot.json"
        save_snapshot(ctx, save_path)
        loaded = load_snapshot(save_path)
        assert loaded["metrics"] == ctx["metrics"]
        assert loaded["decisions"] == ctx["decisions"]
        assert loaded["next_steps"] == ctx["next_steps"]
        assert loaded["test_state"] == ctx["test_state"]
        # file_states should be identical
        assert loaded["file_states"] == ctx["file_states"]

    def test_save_creates_parent_dirs(self, tmp_path):
        ctx = {"kind": "session_snapshot_context", "test": True}
        deep_path = tmp_path / "a" / "b" / "c" / "snapshot.json"
        save_snapshot(ctx, deep_path)
        assert deep_path.exists()

    def test_full_workflow(self, sample_project):
        """Simulate session end → save → new session → load → diff."""
        # Session 1: capture state
        snap1 = session_snapshot_context(
            sample_project,
            decisions=["Used pattern A"],
            next_steps=["Add feature B"],
            test_state={"passed": 5, "failed": 0, "skipped": 0},
        )
        save_path = sample_project / ".session_snapshot.json"
        save_snapshot(snap1, save_path)

        # --- "time passes", code changes ---
        (sample_project / "src" / "feature_b.py").write_text("def feature_b():\n    pass\n")

        # Session 2: load previous, capture new state with diff
        prev = load_snapshot(save_path)
        snap2 = session_snapshot_context(
            sample_project,
            previous_snapshot=prev,
            decisions=["Built feature B"],
            test_state={"passed": 7, "failed": 0, "skipped": 0},
        )

        assert "src/feature_b.py" in snap2["changes_since"]["added"]
        assert snap2["metrics"]["file_count"] == 8  # 6 + .session_snapshot.json + feature_b.py


# ---------------------------------------------------------------------------
# TestOutputFormat
# ---------------------------------------------------------------------------

class TestOutputFormat:
    """Verify output_format dispatch."""

    def test_dict_returns_dict(self, sample_project):
        result = session_snapshot_context(sample_project, output_format="dict")
        assert isinstance(result, dict)

    def test_markdown_returns_string(self, sample_project):
        result = session_snapshot_context(sample_project, output_format="markdown")
        assert isinstance(result, str)
        assert result.startswith("# Session Snapshot:")

    def test_invalid_format_raises(self, sample_project):
        with pytest.raises(ValueError, match="output_format"):
            session_snapshot_context(sample_project, output_format="xml")

    def test_invalid_root_raises(self):
        with pytest.raises(ValueError, match="existing directory"):
            session_snapshot_context("/nonexistent/path/that/does/not/exist")


# ---------------------------------------------------------------------------
# TestRender
# ---------------------------------------------------------------------------

class TestRender:
    """Verify markdown renderer."""

    def test_basic_render(self, sample_project):
        ctx = session_snapshot_context(sample_project)
        md = render_session_snapshot_report(ctx)
        assert "# Session Snapshot:" in md
        assert "Codebase State" in md

    def test_render_with_all_sections(self, sample_project):
        ctx = session_snapshot_context(
            sample_project,
            decisions=["Used X"],
            rejected_alternatives=["Rejected Y"],
            open_questions=["What about Z?"],
            next_steps=["Do A", "Do B"],
            session_notes="Everything works.",
            test_state={"passed": 5, "failed": 0, "skipped": 1},
        )
        md = render_session_snapshot_report(ctx)
        assert "Decisions Made" in md
        assert "Rejected Alternatives" in md
        assert "Open Questions" in md
        assert "Next Steps" in md
        assert "Session Notes" in md
        assert "Test State" in md
        assert "PASSING" in md

    def test_render_with_changes(self, sample_project):
        prev = session_snapshot_context(sample_project)
        (sample_project / "new.py").write_text("# new\n")
        ctx = session_snapshot_context(sample_project, previous_snapshot=prev)
        md = render_session_snapshot_report(ctx)
        assert "Changes Since Last Snapshot" in md
        assert "new.py" in md

    def test_render_failing_tests(self, sample_project):
        ctx = session_snapshot_context(sample_project, test_state={"passed": 8, "failed": 3, "skipped": 0})
        md = render_session_snapshot_report(ctx)
        assert "FAILING" in md

    def test_render_missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_session_snapshot_report({"kind": "session_snapshot_context"})


# ---------------------------------------------------------------------------
# TestDogFood
# ---------------------------------------------------------------------------

class TestDogFood:
    """Run against the real odibi_anchor project."""

    ROOT = str(Path(__file__).resolve().parent.parent)

    def test_real_project(self):
        ctx = session_snapshot_context(self.ROOT, subject="odibi_anchor")
        assert ctx["metrics"]["file_count"] > 30
        assert ctx["metrics"]["py_file_count"] > 20
        assert ctx["metrics"]["total_lines"] > 10000
        # File states should have hashes
        for path, state in list(ctx["file_states"].items())[:5]:
            assert len(state["hash"]) == 12

    def test_real_project_json_serializable(self):
        ctx = session_snapshot_context(self.ROOT, decisions=["test"])
        # Must be JSON-serializable for save/load
        serialized = json.dumps(ctx, default=str)
        assert len(serialized) > 1000



# ---------------------------------------------------------------------------
# Conventions Tests (v0.3.1)
# ---------------------------------------------------------------------------


class TestConventions:
    """Tests for the conventions parameter."""

    def test_conventions_none_by_default(self, sample_project):
        """Conventions defaults to empty dict when not provided."""
        ctx = session_snapshot_context(str(sample_project))
        assert ctx["conventions"] == {}

    def test_conventions_populated(self, sample_project):
        """Conventions are stored in output when provided."""
        conventions = {
            "file_naming": "{module}_context.py",
            "test_naming": "tests/{pkg}/test_{module}_context.py",
            "output_contract": "kind, subject, summary, metrics",
            "imports": "use _utils/contract.py for validate_output_format",
        }
        ctx = session_snapshot_context(str(sample_project), conventions=conventions)
        assert ctx["conventions"] == conventions
        assert any("4 convention(s)" in f for f in ctx["findings"])

    def test_conventions_persist_through_save_load(self, sample_project):
        """Conventions survive save_snapshot -> load_snapshot round-trip."""
        conventions = {"style": "snake_case functions"}
        ctx = session_snapshot_context(str(sample_project), conventions=conventions)
        snap_path = str(sample_project / ".snapshot.json")
        save_snapshot(ctx, snap_path)
        loaded = load_snapshot(snap_path)
        assert loaded["conventions"] == conventions

    def test_conventions_in_markdown(self, sample_project):
        """Conventions section appears in markdown output."""
        ctx = session_snapshot_context(
            str(sample_project),
            conventions={"naming": "snake_case"},
        )
        md = render_session_snapshot_report(ctx)
        assert "## Conventions" in md
        assert "**naming:**" in md

    def test_conventions_json_serializable(self, sample_project):
        """Conventions output passes json.dumps."""
        import json
        ctx = session_snapshot_context(
            str(sample_project),
            conventions={"a": "b", "c": "d"},
        )
        json.dumps(ctx)  # Should not raise

    def test_conventions_empty_dict_no_finding(self, sample_project):
        """Empty conventions dict does not add a finding."""
        ctx = session_snapshot_context(str(sample_project), conventions={})
        assert not any("convention" in f.lower() for f in ctx["findings"])
