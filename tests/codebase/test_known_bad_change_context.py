"""Tests for known_bad_change_context."""

import json

import pytest

from odibi_anchor.codebase._memory_db import close_db, get_db
from odibi_anchor.codebase._memory_db import insert_memory as _db_insert
from odibi_anchor.codebase.known_bad_change_context import (
    _check_static_patterns,
    known_bad_change_context,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def guardrail_root(tmp_path):
    """Project root with memory entries for guardrail testing."""
    db = str(tmp_path / "test_memory.db")
    entries = [
        {"project": "test", "type": "failure_pattern",
         "content": "Renaming a parameter without scanning call sites caused TypeError",
         "related_files": ["src/mylib/utils.py"],
         "tags": ["rename_parameter", "refactor", "auto-learned"],
         "confidence": 0.9, "status": "confirmed",
         "evidence": {"error_text": "TypeError: unexpected keyword argument",
                      "fix": "Scan call sites before renaming"}},
        {"project": "test", "type": "gotcha",
         "content": "Adding imports to __init__.py requires updating __all__",
         "related_files": ["src/**/__init__.py"],
         "tags": ["imports", "exports"],
         "confidence": 0.5, "status": "candidate"},
        {"project": "test", "type": "gotcha",
         "content": "Editing contract.py requires updating test_output_schema.py",
         "related_files": ["src/**/_utils/contract.py"],
         "tags": ["contract", "testing"],
         "confidence": 0.3, "status": "candidate"},
    ]
    ids = []
    for e in entries:
        result = _db_insert(db, project=e["project"], type=e["type"], content=e["content"],
                   related_files=e.get("related_files"), tags=e.get("tags"),
                   confidence=e.get("confidence", 0.5), status=e.get("status", "candidate"),
                   evidence=e.get("evidence"))
        ids.append(result["id"])
    # Historical confirmed state remains inspectable; this fixture does not mint authority.
    connection = get_db(db)
    connection.execute(
        "UPDATE memories SET status='confirmed',confidence=0.9,confirmation_count=3,last_confirmed=? WHERE id=?",
        ("2026-01-01T00:00:00Z", ids[0]),
    )
    connection.commit()
    yield (tmp_path, db)
    close_db(db)


# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self, guardrail_root):
        ctx = known_bad_change_context(str(guardrail_root[0]), db_path=guardrail_root[1], project="test")
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, guardrail_root):
        ctx = known_bad_change_context(str(guardrail_root[0]), db_path=guardrail_root[1], project="test")
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, guardrail_root):
        ctx = known_bad_change_context(str(guardrail_root[0]), db_path=guardrail_root[1], project="test")
        assert ctx["kind"] == "known_bad_change_context"

    def test_json_serializable(self, guardrail_root):
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/utils.py"],
            action="rename_parameter",
        )
        json.dumps(ctx)

    def test_markdown_output(self, guardrail_root):
        md = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test", output_format="markdown"
        )
        assert isinstance(md, str)
        assert "Guardrail" in md


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_no_memory_file_returns_ok(self, tmp_path):
        """Bare tmp_path with no JSONL file → status ok."""
        ctx = known_bad_change_context(str(tmp_path))
        assert ctx["metrics"]["status"] == "ok"

    def test_no_matches_returns_ok(self, guardrail_root):
        """Unrelated files and action → no matches → ok."""
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["totally/unrelated.py"],
            action="add_import",
        )
        assert ctx["metrics"]["status"] == "ok"

    def test_warn_on_similar_failure(self, guardrail_root):
        """m0001 should match on file + action overlap → warn or block."""
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/utils.py"],
            action="rename_parameter",
        )
        assert ctx["metrics"]["status"] in ("warn", "block")

    def test_block_on_confirmed_high_confidence(self, guardrail_root):
        """m0001 has confidence=0.9 and confirmation_count=3 → block when matched."""
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/utils.py"],
            action="rename_parameter",
        )
        # m0001 matches on file AND action, has confidence >= 0.8 and confirmation_count >= 2
        assert ctx["metrics"]["status"] == "block"
        assert ctx["metrics"]["blocking_count"] >= 1

    def test_empty_inputs_no_crash(self, guardrail_root):
        """Call with no optional args → no exception, status ok."""
        ctx = known_bad_change_context(str(guardrail_root[0]), db_path=guardrail_root[1], project="test")
        assert ctx["metrics"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoring:
    def test_file_overlap_increases_score(self, guardrail_root):
        """Entries matching changed_files via glob are returned in matched_memories."""
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/utils.py"],
        )
        matched = ctx.get("matched_memories", [])
        matched_contents = [m.get("content", "") for m in matched]
        assert any("Renaming" in c or "utils.py" in str(m.get("related_files", []))
                   for m, c in zip(matched, matched_contents))

    def test_action_overlap_increases_score(self, guardrail_root):
        """Entries with matching action tag are returned."""
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            action="rename_parameter",
        )
        matched = ctx.get("matched_memories", [])
        matched_contents = [m.get("content", "") for m in matched]
        assert any("Renaming" in c or "rename_parameter" in str(m.get("tags", []))
                   for m, c in zip(matched, matched_contents))

    def test_diff_tokens_extracted(self, guardrail_root):
        """Proposed diff with function names → relevant entries picked up."""
        diff = (
            "--- a/src/mylib/utils.py\n"
            "+++ b/src/mylib/utils.py\n"
            "-def old_function(param_name):\n"
            "+def old_function(new_param_name):\n"
            "-    # Renaming a parameter without scanning call sites\n"
            "+    # Renamed parameter\n"
        )
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            proposed_diff=diff,
            changed_files=["src/mylib/utils.py"],
        )
        matched = ctx.get("matched_memories", [])
        # Should find the "Renaming a parameter" entry via diff token overlap
        assert any("Renaming" in m.get("content", "") for m in matched)

    def test_low_score_filtered(self, guardrail_root):
        """m0003 (unrelated files, low confidence) should not appear for unrelated changes."""
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/views.py"],
            action="add_function",
        )
        matched_ids = [m["id"] for m in ctx.get("matched_memories", [])]
        # Low-confidence unrelated entry should not appear
        assert not any("contract.py" in m.get("content", "") for m in ctx.get("matched_memories", []))


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_includes_status(self, guardrail_root):
        """Markdown output contains status text."""
        md = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test", output_format="markdown"
        )
        assert any(s in md for s in ("OK", "WARNING", "BLOCKED"))

    def test_render_includes_matched_memories(self, guardrail_root):
        """When matches exist, markdown contains Matched Memories section."""
        md = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/utils.py"],
            action="rename_parameter",
            output_format="markdown",
        )
        assert "Matched Memories" in md


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_metrics_has_status_field(self, guardrail_root):
        ctx = known_bad_change_context(str(guardrail_root[0]), db_path=guardrail_root[1], project="test")
        assert ctx["metrics"]["status"] in ("ok", "warn", "block")

    def test_metrics_counts(self, guardrail_root):
        ctx = known_bad_change_context(
            str(guardrail_root[0]), db_path=guardrail_root[1], project="test",
            changed_files=["src/mylib/utils.py"],
            action="rename_parameter",
        )
        assert isinstance(ctx["metrics"]["memories_checked"], int)
        assert isinstance(ctx["metrics"]["memories_matched"], int)
        assert isinstance(ctx["metrics"]["blocking_count"], int)
        assert isinstance(ctx["metrics"]["warning_count"], int)


class TestStaticApplicability:
    def test_generic_prose_does_not_activate_spark_or_databricks_rules(self, tmp_path):
        text = "# Spark prose: df.collect(), /dbfs/input, and Unity Catalog\n"

        matches = _check_static_patterns(
            text,
            root=tmp_path,
            file_path="src/generic.py",
            changed_files=["src/generic.py"],
        )

        assert matches == []

    @pytest.mark.parametrize(
        ("text", "changed_files", "task_type", "expected"),
        [
            ("from pyspark.sql import SparkSession\nrows = df.collect()\n", ["src/job.py"], None, "collect_large"),
            ("rows = df.collect()\n", ["jobs/spark/load.py"], None, "collect_large"),
            ("path = '/dbfs/input'\n", ["src/job.py"], "databricks", "dbfs_mount_path"),
        ],
    )
    def test_concrete_import_path_or_task_domain_activates_platform_rule(
        self, tmp_path, text, changed_files, task_type, expected,
    ):
        matches = _check_static_patterns(
            text,
            root=tmp_path,
            file_path=changed_files[0],
            changed_files=changed_files,
            task_type=task_type,
        )

        assert expected in {match["id"] for match in matches}

    def test_declared_dependency_activates_spark_rule(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pyspark>=3.5"]\n', encoding="utf-8",
        )

        matches = _check_static_patterns(
            "rows = df.collect()\n",
            root=tmp_path,
            file_path="src/job.py",
            changed_files=["src/job.py"],
        )

        assert "collect_large" in {match["id"] for match in matches}

    def test_secret_effect_and_evidence_controls_ignore_suppressions(self, tmp_path):
        patterns = [
            {"id": "secret", "pattern": "SECRET", "message": "secret", "severity": "warn",
             "category": "style", "control_class": "secret"},
            {"id": "effect", "pattern": "EFFECT", "message": "effect", "severity": "warn",
             "category": "style", "control_class": "effect"},
            {"id": "evidence", "pattern": "EVIDENCE", "message": "evidence", "severity": "warn",
             "category": "style", "control_class": "evidence"},
            {"id": "advisory", "pattern": "ADVISORY", "message": "advisory", "severity": "warn",
             "category": "style"},
        ]

        matches = _check_static_patterns(
            "SECRET EFFECT EVIDENCE ADVISORY",
            root=tmp_path,
            patterns=patterns,
            suppress_categories=["style"],
            suppress_ids=[rule["id"] for rule in patterns],
        )

        assert {match["id"] for match in matches} == {"secret", "effect", "evidence"}
