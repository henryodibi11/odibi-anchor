"""Tests for odibi_anchor._dispatcher._memory — memory dispatch functions."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import odibi_anchor._dispatcher._memory as _memory_mod

_memory_tags = _memory_mod._memory_tags
_auto_confirm_surfaced = _memory_mod._auto_confirm_surfaced
_auto_confirm_eligible = _memory_mod._auto_confirm_eligible
_memory_stats = _memory_mod._memory_stats
_render_memory_stats = _memory_mod._render_memory_stats
_new_session = _memory_mod._new_session
_append_notebook_cells = _memory_mod._append_notebook_cells
append_gate_to_notebook = _memory_mod.append_gate_to_notebook
append_learn_to_notebook = _memory_mod.append_learn_to_notebook
_md_cell = _memory_mod._md_cell
_code_cell = _memory_mod._code_cell


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sample_entries():
    """Memory entries as returned by _db_query_memories."""
    return [
        {
            "id": "entry-001",
            "type": "gotcha",
            "content": "Always check for None before calling .split()",
            "tags": ["protective", "had-errors"],
            "confidence": 0.9,
            "status": "candidate",
            "use_count": 5,
        },
        {
            "id": "entry-002",
            "type": "convention",
            "content": "Use build_base_context for all contract dicts",
            "tags": ["protective"],
            "confidence": 0.7,
            "status": "active",
            "use_count": 2,
        },
    ]


@pytest.fixture
def notebook(tmp_path):
    """Create a minimal session notebook."""
    nb_path = tmp_path / "test_session.ipynb"
    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Test Session\n"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    nb_path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return str(nb_path)


@pytest.fixture
def memory_db(tmp_path):
    """Create a real temp SQLite DB with the memories table."""
    db_path = str(tmp_path / "test_memory.db")
    from odibi_anchor.codebase._memory_db import get_db

    conn = get_db(db_path)
    now = "2026-01-01T00:00:00+00:00"
    rows = [
        ("id-1", "proj_a", "gotcha", "Always check for None before calling .split() on user input",
         '["protective"]', "candidate", 3, 1, 8),
        ("id-2", "proj_a", "convention", "Use build_base_context for all contract dicts in dispatcher",
         '["protective"]', "active", 1, 2, 6),
        ("id-3", "proj_b", "discovery", "Foreign secret content", '["protective"]',
         "confirmed", 0, 3, 2),
    ]
    conn.executemany(
        "INSERT INTO memories(id,project,type,content,tags,status,created,last_used,"
        "applied_count,confirmation_count,surface_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(entry_id, project, entry_type, content, tags, status, now, now, applied, confirmed, surfaced)
         for entry_id, project, entry_type, content, tags, status, applied, confirmed, surfaced in rows],
    )
    conn.commit()
    return db_path


# ── TestMemoryTags ───────────────────────────────────────────────────────


class TestMemoryTags:
    """Tests for _memory_tags — tag-based memory query."""

    def test_no_tags_returns_hint_summary(self):
        result = _memory_tags()
        assert isinstance(result, dict)
        assert result["kind"] == "memory_tags"
        assert "No tags specified" in result["summary"]
        assert result["metrics"]["matched"] == 0

    def test_scope_is_required_before_content_query(self):
        with pytest.raises(ValueError, match="active project"):
            _memory_tags(tags=["protective"])

    def test_foreign_project_content_is_not_disclosed(self, memory_db):
        result = _memory_tags(
            tags=["protective"], project="proj_a", db_path=memory_db,
        )
        assert {entry["id"] for entry in result["samples"]["entries"]} == {"id-1", "id-2"}
        assert "Foreign secret" not in json.dumps(result)

    def test_with_tags_returns_matching_entries(self, sample_entries):
        with patch.object(_memory_mod, "_db_query_memories", return_value=sample_entries):
            result = _memory_tags(tags=["protective"], project="proj_a")
        assert isinstance(result, dict)
        assert result["kind"] == "memory_tags"
        assert "2 entries" in result["summary"]
        assert result["metrics"]["matched"] == 2
        assert len(result["samples"]["entries"]) == 2
        assert result["samples"]["entries"][0]["id"] == "entry-001"

    def test_markdown_output_format(self, sample_entries):
        with patch.object(_memory_mod, "_db_query_memories", return_value=sample_entries):
            result = _memory_tags(tags=["protective"], project="proj_a", output_format="markdown")
        assert isinstance(result, str)
        assert "Memory Tags Query" in result
        assert "protective" in result
        assert "gotcha" in result

    def test_dict_output_format_has_standard_keys(self, sample_entries):
        with patch.object(_memory_mod, "_db_query_memories", return_value=sample_entries):
            result = _memory_tags(tags=["protective"], project="proj_a", output_format="dict")
        assert isinstance(result, dict)
        for key in ("kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"):
            assert key in result

    def test_no_results_for_tags(self):
        with patch.object(_memory_mod, "_db_query_memories", return_value=[]):
            result = _memory_tags(tags=["nonexistent"], project="proj_a")
        assert result["metrics"]["matched"] == 0
        assert "No entries match" in result["findings"][0]

    def test_tags_as_positional_args(self, sample_entries):
        with patch.object(_memory_mod, "_db_query_memories", return_value=sample_entries):
            result = _memory_tags("protective", "had-errors", project="proj_a")
        assert result["metrics"]["tags_queried"] == ["protective", "had-errors"]

    def test_content_truncated_to_200(self):
        long_content = "x" * 500
        entries = [
            {"id": "long-1", "type": "gotcha", "content": long_content,
             "tags": ["t"], "confidence": 0.5, "status": "candidate"},
        ]
        with patch.object(_memory_mod, "_db_query_memories", return_value=entries):
            result = _memory_tags(tags=["t"], project="proj_a")
        assert len(result["samples"]["entries"][0]["content"]) == 200

    def test_markdown_no_results_shows_message(self):
        with patch.object(_memory_mod, "_db_query_memories", return_value=[]):
            result = _memory_tags(tags=["nothing"], project="proj_a", output_format="markdown")
        assert "No matching entries found" in result


# ── TestAutoConfirmSurfaced ──────────────────────────────────────────────


class TestAutoConfirmSurfaced:
    """Exposure alone must never promote memory authority."""

    def test_is_no_op_even_for_frequently_surfaced_candidate(self):
        result = {"entries": [{"id": "e1", "use_count": 999, "status": "candidate"}]}
        assert _auto_confirm_surfaced(result, db_path="unused.db") is None


# ── TestAutoConfirmEligible ──────────────────────────────────────────────


class TestAutoConfirmEligible:
    """Bulk promotion remains disabled without explicit confirmation evidence."""

    def test_returns_zero_and_does_not_promote(self, memory_db):
        assert _auto_confirm_eligible(db_path=memory_db) == 0
        conn = sqlite3.connect(memory_db)
        statuses = dict(conn.execute("SELECT id, status FROM memories"))
        conn.close()
        assert statuses["id-1"] == "candidate"
        assert statuses["id-2"] == "active"


# ── TestMemoryStats ──────────────────────────────────────────────────────


class TestMemoryStats:
    """Tests for _memory_stats — DB stats summary."""

    def test_dict_output(self, memory_db):
        result = _memory_stats(db_path=memory_db, project="proj_a", output_format="dict")
        assert isinstance(result, dict)
        assert result["kind"] == "memory_stats"
        assert result["metrics"]["total"] == 2
        assert "by_status" in result
        assert "by_type" in result
        assert "by_project" in result
        assert "top_by_explicit_evidence" in result
        leader = result["top_by_explicit_evidence"][0]
        assert leader["application_count"] == 0
        assert leader["helpful_count"] == 0
        assert leader["negative_count"] == 0
        assert "auto_confirm" not in json.dumps(result).lower()

    def test_markdown_output(self, memory_db):
        result = _memory_stats(db_path=memory_db, project="proj_a", output_format="markdown")
        assert isinstance(result, str)
        assert "Memory Stats" in result
        assert "By Status" in result
        assert "By Type" in result
        assert "Explicit Evidence" in result
        assert "Auto-Confirm" not in result

    def test_status_breakdown(self, memory_db):
        result = _memory_stats(db_path=memory_db, project="proj_a", output_format="dict")
        assert result["by_status"]["candidate"] == 1
        assert result["by_status"]["active"] == 1
        assert "confirmed" not in result["by_status"]

    def test_project_breakdown(self, memory_db):
        result = _memory_stats(db_path=memory_db, project="proj_a", output_format="dict")
        assert result["by_project"]["proj_a"] == 2
        assert "proj_b" not in result["by_project"]


# ── TestRenderMemoryStats ────────────────────────────────────────────────


class TestRenderMemoryStats:
    """Tests for _render_memory_stats — pure render function."""

    @pytest.fixture
    def stats_ctx(self):
        return {
            "total": 42,
            "by_status": {"candidate": 20, "confirmed": 15, "active": 7},
            "by_type": {"gotcha": 10, "convention": 8, "discovery": 24},
            "by_project": {"proj_a": 30, "proj_b": 12},
            "top_by_explicit_evidence": [
                {"id": "abcdef12-3456", "type": "gotcha", "status": "confirmed",
                 "application_count": 15, "helpful_count": 4, "negative_count": 1,
                 "confidence": 0.9, "content_preview": "Always check None"},
                {"id": "deadbeef-1234", "type": "convention", "status": "active",
                 "application_count": 8, "helpful_count": 2, "negative_count": 0,
                 "confidence": 0.7, "content_preview": "Use build_base_context"},
            ],
        }

    def test_contains_header(self, stats_ctx):
        md = _render_memory_stats(stats_ctx)
        assert "Memory Stats (42 entries)" in md

    def test_contains_status_breakdown(self, stats_ctx):
        md = _render_memory_stats(stats_ctx)
        assert "**candidate**: 20" in md
        assert "**confirmed**: 15" in md
        assert "**active**: 7" in md

    def test_contains_type_breakdown(self, stats_ctx):
        md = _render_memory_stats(stats_ctx)
        assert "gotcha: 10" in md
        assert "convention: 8" in md

    def test_contains_project_breakdown(self, stats_ctx):
        md = _render_memory_stats(stats_ctx)
        assert "By Project" in md
        assert "proj_a: 30" in md

    def test_single_project_skips_project_section(self):
        ctx = {
            "total": 10,
            "by_status": {"confirmed": 10},
            "by_type": {"gotcha": 10},
            "by_project": {"only_one": 10},
            "top_by_explicit_evidence": [],
        }
        md = _render_memory_stats(ctx)
        assert "By Project" not in md

    def test_contains_explicit_evidence_without_auto_confirm_claims(self, stats_ctx):
        md = _render_memory_stats(stats_ctx)
        assert "Top Entries by Explicit Evidence" in md
        assert "Applications" in md
        assert "Helpful" in md
        assert "Negative" in md
        assert "Auto-Confirm" not in md

    def test_contains_top_entries_table(self, stats_ctx):
        md = _render_memory_stats(stats_ctx)
        assert "Top Entries by Explicit Evidence" in md
        assert "abcdef12" in md
        assert "| 15 | 4 | 1 |" in md
        assert "Always check None" in md


# ── TestNewSession ───────────────────────────────────────────────────────


class TestNewSession:
    """Tests for _new_session — session notebook generation."""

    def test_inline_mode_returns_contract(self, tmp_path):
        result = _new_session(
            root=str(tmp_path),
            anchor_root=str(tmp_path),
            name="test_feature",
            inline=True,
        )
        assert isinstance(result, dict)
        assert result["kind"] == "new_session"
        assert result["metrics"]["mode"] == "inline"
        assert result["metrics"]["notebook_created"] is False
        assert "test_feature" in result["summary"]

    def test_inline_mode_is_default(self, tmp_path):
        result = _new_session(
            root=str(tmp_path),
            anchor_root=str(tmp_path),
            name="test_feature",
        )
        assert result["metrics"]["mode"] == "inline"

    def test_name_too_short_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="BLOCKED"):
            _new_session(root=str(tmp_path), anchor_root=str(tmp_path), name="ab")

    def test_no_name_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="BLOCKED"):
            _new_session(root=str(tmp_path), anchor_root=str(tmp_path))

    def test_file_mode_creates_notebook(self, tmp_path):
        root = tmp_path / "my_project"
        root.mkdir()
        anchor_root = tmp_path / "anchor"
        anchor_root.mkdir()
        result = _new_session(
            root=str(root),
            anchor_root=str(anchor_root),
            name="sprint_one",
            inline=False,
            features=2,
        )
        assert isinstance(result, dict)
        nb_path = result["metrics"]["notebook_path"]
        assert result["metrics"]["notebook_created"] is True
        assert Path(nb_path).exists()
        with open(nb_path, encoding="utf-8") as f:
            nb = json.load(f)
        assert nb["nbformat"] == 4
        assert len(nb["cells"]) > 0
        assert result["metrics"]["feature_blocks"] == 2
        source = "\n".join(
            "".join(cell.get("source", [])) for cell in nb["cells"]
        )
        required_order = (
            'anchor("status")', 'anchor("audit_history")', 'anchor("new_session"', 'anchor("task"',
            "task_result['memory_context']", 'anchor("skill_loaded"',
            'anchor("known_bad"', 'anchor("preflight")', 'anchor("test")', 'anchor("review")',
            'anchor("gate")', 'anchor("learning", "assess"',
        )
        positions = [source.index(action) for action in required_order]
        assert positions == sorted(positions)
        assert 'anchor("memory")' not in source
        assert 'outcome="nothing_reusable_learned"' in source
        assert '"content": "[what you genuinely learned]"' not in source

    def test_file_mode_routes_notebook_to_artifact_root(self, tmp_path):
        artifact_root = tmp_path / "workspace" / "projects" / "alpha"
        result = _new_session(
            root=str(tmp_path / "external-source"),
            anchor_root=str(tmp_path),
            artifact_root=str(artifact_root),
            name="artifact_routing",
            inline=False,
        )

        notebook_path = Path(result["metrics"]["notebook_path"])
        assert notebook_path.parent == artifact_root / "notebooks"
        assert notebook_path.is_file()

    def test_file_mode_notebook_structure(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        anchor_root = tmp_path / "anchor"
        anchor_root.mkdir()
        result = _new_session(
            root=str(root),
            anchor_root=str(anchor_root),
            name="test_nb",
            inline=False,
            features=1,
        )
        nb_path = result["metrics"]["notebook_path"]
        with open(nb_path, encoding="utf-8") as f:
            nb = json.load(f)
        # Check cells contain expected content
        all_sources = []
        for c in nb["cells"]:
            all_sources.extend(c["source"])
        combined = "".join(all_sources)
        assert "Session:" in combined
        assert "Feature 1" in combined
        assert "from odibi_anchor.bootstrap import init" in combined
        assert "agent_init.py" not in combined
        assert 'anchor("gate")' in combined
        assert 'anchor("learning", "assess"' in combined

    def test_file_mode_bootstrap_restores_project_and_target(self, tmp_path):
        target_root = tmp_path / "external source"
        target_root.mkdir()
        artifact_root = tmp_path / "workspace" / "projects" / "alpha"
        result = _new_session(
            root=str(target_root),
            anchor_root=str(tmp_path / "anchor"),
            artifact_root=str(artifact_root),
            project="alpha",
            name="restore_routing",
            inline=False,
            features=1,
        )

        notebook = json.loads(Path(result["metrics"]["notebook_path"]).read_text(encoding="utf-8"))
        bootstrap_source = "".join(notebook["cells"][2]["source"])
        assert "from odibi_anchor.bootstrap import init" in bootstrap_source
        assert f"root={str(target_root)!r}" in bootstrap_source
        assert "project='alpha'" in bootstrap_source
        assert "agent_init.py" not in bootstrap_source

    def test_name_with_spaces_normalized(self, tmp_path):
        result = _new_session(
            root=str(tmp_path),
            anchor_root=str(tmp_path),
            name="my feature name",
            inline=True,
        )
        assert result["metrics"]["session_name"] == "my_feature_name"

    def test_name_as_positional_arg(self, tmp_path):
        result = _new_session(
            str(tmp_path), str(tmp_path), "pos_name", inline=True,
        )
        assert result["metrics"]["session_name"] == "pos_name"


# ── TestAppendNotebookCells ──────────────────────────────────────────────


class TestAppendNotebookCells:
    """Tests for _append_notebook_cells."""

    def test_appends_cells_correctly(self, notebook):
        cells = [_md_cell(["## Added\n"]), _code_cell(["print('hello')\n"])]
        assert _append_notebook_cells(notebook, cells) is True
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 3
        assert nb["cells"][1]["cell_type"] == "markdown"
        assert nb["cells"][2]["cell_type"] == "code"

    def test_returns_false_on_missing_file(self):
        assert _append_notebook_cells("/nonexistent/path.ipynb", []) is False

    def test_appends_empty_list_preserves_notebook(self, notebook):
        assert _append_notebook_cells(notebook, []) is True
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 1


# ── TestAppendGateToNotebook ─────────────────────────────────────────────


class TestAppendGateToNotebook:
    """Tests for append_gate_to_notebook — gate result cell injection."""

    def test_appends_gate_cells(self, notebook):
        gate_result = {
            "metrics": {"score": 9, "max_score": 10, "rating": "excellent",
                        "all_verified": True},
            "findings": ["All tests passed", "No lint errors"],
        }
        assert append_gate_to_notebook(notebook, gate_result, {"a.py"}, feature_num=2)
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 4  # original + 3 gate cells
        combined = "".join("".join(c["source"]) for c in nb["cells"])
        assert "Feature 2" in combined
        assert "PASSED" in combined
        assert "a.py" in combined

    def test_partial_gate(self, notebook):
        gate_result = {
            "metrics": {"score": 5, "max_score": 10, "rating": "fair",
                        "all_verified": False},
            "findings": ["Some tests failed"],
        }
        assert append_gate_to_notebook(notebook, gate_result, {"x.py"})
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        combined = "".join("".join(c["source"]) for c in nb["cells"])
        assert "PARTIAL" in combined

    def test_empty_path_returns_false(self):
        assert append_gate_to_notebook("", {}, set()) is False

    def test_none_path_returns_false(self):
        assert append_gate_to_notebook(None, {}, set()) is False


# ── TestAppendLearnToNotebook ────────────────────────────────────────────


class TestAppendLearnToNotebook:
    """Tests for append_learn_to_notebook — learn result cell injection."""

    def test_appends_learn_cells(self, notebook):
        learn_result = {"metrics": {"memories_added": 4}}
        events = [
            {"type": "discovery", "detail": "Found edge case in parsing"},
            {"type": "decision", "detail": "Chose regex over AST"},
        ]
        assert append_learn_to_notebook(notebook, learn_result, session_events=events)
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 2
        text = "".join(nb["cells"][1]["source"])
        assert "discovery" in text
        assert "Found edge case" in text
        assert "4 memory entries" in text

    def test_empty_path_returns_false(self):
        assert append_learn_to_notebook("", {}) is False

    def test_no_events_still_appends(self, notebook):
        learn_result = {"metrics": {"memories_added": 1}}
        assert append_learn_to_notebook(notebook, learn_result)
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 2

    def test_non_dict_events_skipped(self, notebook):
        learn_result = {"metrics": {"memories_added": 0}}
        events = ["not a dict", 42]
        assert append_learn_to_notebook(notebook, learn_result, session_events=events)


# ── TestHelperCells ──────────────────────────────────────────────────────


class TestHelperCells:
    """Tests for _md_cell and _code_cell helper functions."""

    def test_md_cell_structure(self):
        cell = _md_cell(["# Header\n"])
        assert cell["cell_type"] == "markdown"
        assert cell["metadata"] == {}
        assert cell["source"] == ["# Header\n"]

    def test_code_cell_structure(self):
        cell = _code_cell(["print('hi')\n"])
        assert cell["cell_type"] == "code"
        assert cell["metadata"] == {}
        assert cell["source"] == ["print('hi')\n"]
        assert cell["outputs"] == []
        assert cell["execution_count"] is None
