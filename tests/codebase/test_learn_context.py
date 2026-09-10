"""Tests for learn_context."""

import json
import pytest

from odibi_anchor.codebase.learn_context import (
    learn_context,
    render_learn_report,
)
from odibi_anchor.codebase.memory_context import append_memory
from odibi_anchor.codebase._memory_db import close_db




@pytest.fixture
def learn_root(tmp_path):
    """Provides (root_path, db_path) for learn_context tests."""
    db = str(tmp_path / "test_memory.db")
    yield (tmp_path, db)
    close_db(db)

# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_omitted_events_default_to_empty(self, tmp_path):
        ctx = learn_context(str(tmp_path))
        assert ctx["metrics"]["events_processed"] == 0

    def test_returns_dict_by_default(self, tmp_path):
        ctx = learn_context(str(tmp_path), session_events=[])
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, tmp_path):
        ctx = learn_context(str(tmp_path), session_events=[])
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, tmp_path):
        ctx = learn_context(str(tmp_path), session_events=[])
        assert ctx["kind"] == "learn_context"

    def test_json_serializable(self, tmp_path):
        ctx = learn_context(str(tmp_path), session_events=[])
        json.dumps(ctx)

    def test_markdown_output(self, tmp_path):
        md = learn_context(str(tmp_path), session_events=[], output_format="markdown")
        assert isinstance(md, str)
        assert "Compatibility Learning Recovery" in md


# ---------------------------------------------------------------------------
# Learning Behavior
# ---------------------------------------------------------------------------


class TestLearning:
    def test_records_novel_error(self, learn_root):
        """New error→fix is recorded to DB."""
        root, db = learn_root
        events = [
            {"type": "error", "text": "ModuleNotFoundError: No module named 'foo'",
             "resolved": True, "fix": "pip install foo"},
        ]
        ctx = learn_context(str(root), session_events=events, db_path=db, project="test")
        assert ctx["metrics"]["memories_added"] == 1

        # Verify via context output (memories_added list in output)
        memories = ctx.get("memories_added", [])
        assert len(memories) >= 1
        assert any("ModuleNotFoundError" in m.get("content", "") for m in memories)

    def test_skips_known_error(self, learn_root):
        """Existing error not duplicated when content overlaps sufficiently."""
        root, db = learn_root
        # Pre-seed a memory that will match the error text via word overlap
        append_memory(
            str(root),
            entry_type="gotcha",
            content="ModuleNotFoundError No module named foo pip install fix",
            tags=["auto-learned", "error-fix"],
            db_path=db, project="test",
        )

        events = [
            {"type": "error", "text": "ModuleNotFoundError: No module named 'foo'",
             "resolved": True, "fix": "pip install foo"},
        ]
        ctx = learn_context(str(root), session_events=events, db_path=db, project="test")
        # Should NOT add a duplicate since existing entry matches
        assert ctx["metrics"]["memories_added"] == 0

    def test_records_propagation_pattern(self, learn_root):
        """Forgot files recorded as gotcha."""
        root, db = learn_root
        events = [
            {"type": "edit", "file": "contract.py",
             "also_updated": ["test_output.py"],
             "forgot": [".agent_memory.md"]},
        ]
        ctx = learn_context(str(root), session_events=events, db_path=db, project="test")
        assert ctx["metrics"]["memories_added"] == 1

        # Verify via context output
        memories = ctx.get("memories_added", [])
        assert len(memories) >= 1
        assert "contract.py" in memories[0].get("content", "")
        assert ".agent_memory.md" in memories[0].get("content", "")

    def test_discovery_records_with_detail_key(self, learn_root):
        """Regression: the documented/gate-validated event shape uses 'detail',
        but the handlers only read 'text' — so a discovery with detail= silently
        recorded nothing. detail must now insert a memory."""
        root, db = learn_root
        events = [{"type": "discovery",
                   "detail": "pre_join normalizes float/int keys to avoid false zero overlap"}]
        ctx = learn_context(str(root), session_events=events, db_path=db, project="test")
        assert ctx["metrics"]["memories_added"] == 1
        assert "float/int" in ctx["memories_added"][0].get("content", "")

    def test_decision_records_with_detail_key(self, learn_root):
        """General-purpose types (decision/convention/...) also accept detail=."""
        root, db = learn_root
        events = [{"type": "decision",
                   "detail": "expose skill_loaded and orient via the MCP server"}]
        ctx = learn_context(str(root), session_events=events, db_path=db, project="test")
        assert ctx["metrics"]["memories_added"] == 1

    def test_selected_learning_is_tagged_with_problem_id(self, learn_root):
        from odibi_anchor.codebase.memory_context import memory_context

        root, db = learn_root
        learn_context(
            str(root),
            session_events=[{
                "type": "decision",
                "detail": "Scale workers before redesigning the queue protocol",
            }],
            db_path=db,
            project="test",
            problem_id="PRB-2026-0001",
        )
        memories = memory_context(
            str(root),
            tags=["problem:PRB-2026-0001"],
            db_path=db,
            project="test",
            output_format="dict",
        )

        assert len(memories["entries"]) == 1

    def test_logs_telemetry(self, tmp_path):
        """tool_usage written to .telemetry.jsonl."""
        tool_usage = [
            {"tool": "codebase_map_context", "was_helpful": True, "outcome": "oriented"},
            {"tool": "test_focus_context", "was_helpful": False, "outcome": "no tests found"},
        ]
        ctx = learn_context(str(tmp_path), session_events=[], tool_usage=tool_usage)
        assert ctx["metrics"]["telemetry_logged"] == 2

        # Verify on disk
        telemetry_path = tmp_path / ".telemetry.jsonl"
        assert telemetry_path.exists()
        lines = telemetry_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_empty_events(self, tmp_path):
        """No events produces no side effects."""
        ctx = learn_context(str(tmp_path), session_events=[])
        assert ctx["metrics"]["events_processed"] == 0
        assert ctx["metrics"]["memories_added"] == 0
        assert ctx["metrics"]["telemetry_logged"] == 0
