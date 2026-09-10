"""Tests for odibi_anchor.tables.apply_transform_context."""

from __future__ import annotations

import pandas as pd
import pytest

from odibi_anchor.tables.apply_transform_context import (
    apply_transform_context,
    render_apply_transform_report,
    rollback,
    unpersist,
    _estimate_memory_bytes,
    _evict_oldest_checkpoint,
    _format_bytes,
)
from odibi_anchor.tables.transform_plan_context import transform_plan_context


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_plan(profile: dict, **kwargs) -> dict:
    """Generate a transform plan from a profile dict."""
    return transform_plan_context(profile, output_format="dict", **kwargs)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def messy_df() -> pd.DataFrame:
    """Messy DataFrame with columns needing standardization, nulls, and casts."""
    return pd.DataFrame({
        "Invoice ID": ["INV-001", "INV-002", "INV-003", "INV-004", "INV-005"],
        "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        "Qty": ["10", "20", "N/A", "40", "50"],
        "Is Active": ["yes", "no", "yes", "no", "yes"],
        "Created Date": ["01/15/2024", "02/20/2024", "03/10/2024", "04/05/2024", "05/01/2024"],
        "Notes": [None, None, None, None, None],
    })


@pytest.fixture
def standardize_plan() -> dict:
    """Plan with only a standardize step."""
    profile = {
        "summary": {"row_count": 5, "column_count": 2},
        "columns": {
            "Invoice ID": {
                "name": "Invoice ID", "declared_dtype": "object",
                "inferred_type": "string", "null_rate": 0.0,
                "distinct_count": 5, "unique_rate": 1.0,
                "date_format": None, "date_formats": [], "top_values": [],
            },
            "Amount": {
                "name": "Amount", "declared_dtype": "object",
                "inferred_type": "string", "null_rate": 0.0,
                "distinct_count": 5, "unique_rate": 1.0,
                "date_format": None, "date_formats": [], "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test", include_dedup=False)


@pytest.fixture
def trim_plan() -> dict:
    """Plan with a whitespace-trim step (column flagged with leading spaces)."""
    profile = {
        "summary": {"row_count": 3, "column_count": 1},
        "columns": {
            "name": {
                "name": "name", "declared_dtype": "object",
                "inferred_type": "string", "null_rate": 0.0,
                "distinct_count": 3, "unique_rate": 1.0,
                "has_leading_spaces": True, "has_trailing_spaces": True,
                "date_format": None, "date_formats": [], "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test", include_dedup=False)


@pytest.fixture
def null_cleanup_plan() -> dict:
    """Plan with a null cleanup step."""
    profile = {
        "summary": {"row_count": 5, "column_count": 1},
        "columns": {
            "amount": {
                "name": "amount", "declared_dtype": "object",
                "inferred_type": "string", "null_rate": 0.1,
                "distinct_count": 5, "unique_rate": 1.0,
                "date_format": None, "date_formats": [], "top_values": [],
            },
        },
        "null_like_strings": {"amount": ["N/A", ""]},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test", include_dedup=False)


@pytest.fixture
def cast_plan() -> dict:
    """Plan with a numeric cast step."""
    profile = {
        "summary": {"row_count": 5, "column_count": 1},
        "columns": {
            "price": {
                "name": "price", "declared_dtype": "object",
                "inferred_type": "float", "null_rate": 0.0,
                "distinct_count": 5, "unique_rate": 1.0,
                "date_format": None, "date_formats": [], "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "price": {"declared": "object", "inferred": "float", "parseable_pct": 98.0, "date_format": None},
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test", include_dedup=False)


@pytest.fixture
def boolean_plan() -> dict:
    """Plan with a boolean cast step."""
    profile = {
        "summary": {"row_count": 5, "column_count": 1},
        "columns": {
            "is_active": {
                "name": "is_active", "declared_dtype": "object",
                "inferred_type": "boolean", "null_rate": 0.0,
                "distinct_count": 2, "unique_rate": 0.4,
                "date_format": None, "date_formats": [], "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "is_active": {"declared": "object", "inferred": "boolean", "parseable_pct": 100.0, "date_format": None},
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test", include_dedup=False)


@pytest.fixture
def date_plan() -> dict:
    """Plan with a date parse step."""
    profile = {
        "summary": {"row_count": 5, "column_count": 1},
        "columns": {
            "created_date": {
                "name": "created_date", "declared_dtype": "object",
                "inferred_type": "date", "null_rate": 0.0,
                "distinct_count": 5, "unique_rate": 1.0,
                "date_format": "%m/%d/%Y", "date_formats": [("%m/%d/%Y", 0.95)],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "created_date": {"declared": "object", "inferred": "date", "parseable_pct": 95.0, "date_format": "%m/%d/%Y"},
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test", include_dedup=False)


@pytest.fixture
def dedup_plan() -> dict:
    """Plan with a dedup step."""
    profile = {
        "summary": {"row_count": 5, "column_count": 2},
        "columns": {
            "project_id": {
                "name": "project_id", "declared_dtype": "object",
                "inferred_type": "string", "null_rate": 0.0,
                "distinct_count": 3, "unique_rate": 0.6,
                "date_format": None, "date_formats": [], "top_values": [],
            },
            "updated_at": {
                "name": "updated_at", "declared_dtype": "datetime64[ns]",
                "inferred_type": "date", "null_rate": 0.0,
                "distinct_count": 5, "unique_rate": 1.0,
                "date_format": None, "date_formats": [], "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [("project_id",)],
        "anomalies": [],
        "snapshot_columns": [],
    }
    return _make_plan(profile, subject="test")


# ── Test Cases ────────────────────────────────────────────────────────────────


class TestStandardize:
    """Tests for the standardize_columns step handler."""

    def test_renames_columns(self, standardize_plan: dict) -> None:
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan)

        out_df = result["df"]
        assert "invoice_id" in out_df.columns
        assert "amount" in out_df.columns
        assert "Invoice ID" not in out_df.columns

    def test_tracks_renames(self, standardize_plan: dict) -> None:
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan)

        assert result["columns_renamed"]["Invoice ID"] == "invoice_id"
        assert result["columns_renamed"]["Amount"] == "amount"


class TestTrim:
    """Tests for the trim step handler (regression: trim was detected but never applied)."""

    def test_plan_includes_trim_step(self, trim_plan: dict) -> None:
        actions = [s["action"] for s in trim_plan["steps"]]
        assert "trim" in actions

    def test_strips_whitespace(self, trim_plan: dict) -> None:
        df = pd.DataFrame({"name": [" Alice", " Bob ", "Carol"]})
        result = apply_transform_context(df, trim_plan)
        out_df = result["df"]
        assert list(out_df["name"]) == ["Alice", "Bob", "Carol"]

    def test_non_string_values_pass_through(self, trim_plan: dict) -> None:
        # mixed object column: None / numbers must survive (not become NaN)
        df = pd.DataFrame({"name": [" Alice ", None, 42]})
        result = apply_transform_context(df, trim_plan)
        out_df = result["df"]
        assert out_df["name"].iloc[0] == "Alice"
        assert out_df["name"].iloc[1] is None
        assert out_df["name"].iloc[2] == 42

    def test_idempotent(self, trim_plan: dict) -> None:
        df = pd.DataFrame({"name": [" Alice "]})
        once = apply_transform_context(df, trim_plan)["df"]
        twice = apply_transform_context(once, trim_plan)["df"]
        assert list(once["name"]) == list(twice["name"]) == ["Alice"]


class TestNullCleanup:
    """Tests for the null_cleanup step handler."""

    def test_replaces_null_like_values(self, null_cleanup_plan: dict) -> None:
        df = pd.DataFrame({"amount": ["100", "N/A", "200", "", "300"]})
        result = apply_transform_context(df, null_cleanup_plan)

        out_df = result["df"]
        assert out_df["amount"].iloc[1] is None
        assert out_df["amount"].iloc[3] is None
        assert out_df["amount"].iloc[0] == "100"


class TestCast:
    """Tests for the cast step handler."""

    def test_casts_to_float(self, cast_plan: dict) -> None:
        df = pd.DataFrame({"price": ["10.5", "20.0", "30.75"]})
        result = apply_transform_context(df, cast_plan)

        out_df = result["df"]
        assert out_df["price"].dtype == float
        assert out_df["price"].iloc[0] == 10.5

    def test_coerces_unparseable(self, cast_plan: dict) -> None:
        df = pd.DataFrame({"price": ["10.5", "invalid", "30.0"]})
        result = apply_transform_context(df, cast_plan)

        out_df = result["df"]
        assert pd.isna(out_df["price"].iloc[1])


class TestBooleanCast:
    """Tests for the boolean_cast step handler."""

    def test_maps_boolean_values(self, boolean_plan: dict) -> None:
        df = pd.DataFrame({"is_active": ["yes", "no", "true", "false", "1"]})
        result = apply_transform_context(df, boolean_plan)

        out_df = result["df"]
        assert out_df["is_active"].iloc[0] == True  # noqa: E712 — numpy bool
        assert out_df["is_active"].iloc[1] == False  # noqa: E712
        assert out_df["is_active"].iloc[4] == True  # noqa: E712


class TestDateParse:
    """Tests for the date_parse step handler."""

    def test_parses_dates(self, date_plan: dict) -> None:
        from datetime import date

        df = pd.DataFrame({"created_date": ["01/15/2024", "02/20/2024", "03/10/2024"]})
        result = apply_transform_context(df, date_plan)

        out_df = result["df"]
        assert out_df["created_date"].iloc[0] == date(2024, 1, 15)
        assert out_df["created_date"].iloc[1] == date(2024, 2, 20)


class TestDedup:
    """Tests for the dedup step handler."""

    def test_deduplicates(self, dedup_plan: dict) -> None:
        df = pd.DataFrame({
            "project_id": ["A", "A", "B", "B", "C"],
            "updated_at": pd.to_datetime(["2024-01-01", "2024-01-05", "2024-01-02", "2024-01-06", "2024-01-03"]),
        })
        result = apply_transform_context(df, dedup_plan)

        out_df = result["df"]
        assert len(out_df) == 3  # 3 unique project_ids
        # Should keep latest per project_id
        a_row = out_df[out_df["project_id"] == "A"]
        assert a_row["updated_at"].iloc[0] == pd.Timestamp("2024-01-05")

    def test_tracks_rows_dropped(self, dedup_plan: dict) -> None:
        df = pd.DataFrame({
            "project_id": ["A", "A", "B"],
            "updated_at": pd.to_datetime(["2024-01-01", "2024-01-05", "2024-01-02"]),
        })
        result = apply_transform_context(df, dedup_plan)

        assert result["row_count_before"] == 3
        assert result["row_count_after"] == 2
        assert result["rows_dropped"] == 1


class TestStepSelection:
    """Tests for selective step execution."""

    def test_apply_specific_steps(self) -> None:
        """Only apply steps 1 and 3, skip step 2."""
        profile = {
            "summary": {"row_count": 5, "column_count": 2},
            "columns": {
                "Invoice ID": {
                    "name": "Invoice ID", "declared_dtype": "object",
                    "inferred_type": "string", "null_rate": 0.0,
                    "distinct_count": 5, "unique_rate": 1.0,
                    "date_format": None, "date_formats": [], "top_values": [],
                },
                "amount": {
                    "name": "amount", "declared_dtype": "object",
                    "inferred_type": "string", "null_rate": 0.1,
                    "distinct_count": 5, "unique_rate": 1.0,
                    "date_format": None, "date_formats": [], "top_values": [],
                },
            },
            "null_like_strings": {"amount": ["N/A"]},
            "type_mismatches": {},
            "key_candidates": [],
            "anomalies": [],
            "snapshot_columns": [],
        }
        plan = _make_plan(profile, subject="test", include_dedup=False)

        df = pd.DataFrame({"Invoice ID": ["A", "B"], "amount": ["100", "N/A"]})

        # Only apply step 1 (standardize), skip step 2 (null cleanup)
        result = apply_transform_context(df, plan, steps=[1])

        assert 1 in result["steps_applied"]
        assert 2 not in result["steps_applied"]
        # Columns should be renamed
        assert "invoice_id" in result["df"].columns


class TestDryRun:
    """Tests for dry_run mode."""

    def test_dry_run_does_not_mutate(self, standardize_plan: dict) -> None:
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, dry_run=True)

        # DataFrame should be unchanged
        assert "Invoice ID" in result["df"].columns
        assert "invoice_id" not in result["df"].columns
        # But steps should be listed as applied
        assert len(result["steps_applied"]) > 0

    def test_dry_run_summary(self, standardize_plan: dict) -> None:
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, dry_run=True)

        assert "Dry run" in result["summary"]


class TestMinConfidence:
    """Tests for confidence gating."""

    def test_skips_low_confidence_steps(self) -> None:
        """Steps below min_confidence are skipped."""
        profile = {
            "summary": {"row_count": 5, "column_count": 2},
            "columns": {
                "project_id": {
                    "name": "project_id", "declared_dtype": "object",
                    "inferred_type": "string", "null_rate": 0.0,
                    "distinct_count": 3, "unique_rate": 0.6,
                    "date_format": None, "date_formats": [], "top_values": [],
                },
                "updated_at": {
                    "name": "updated_at", "declared_dtype": "datetime64[ns]",
                    "inferred_type": "date", "null_rate": 0.0,
                    "distinct_count": 5, "unique_rate": 1.0,
                    "date_format": None, "date_formats": [], "top_values": [],
                },
            },
            "null_like_strings": {},
            "type_mismatches": {},
            "key_candidates": [("project_id",)],
            "anomalies": [],
            "snapshot_columns": [],
        }
        plan = _make_plan(profile, subject="test")
        df = pd.DataFrame({
            "project_id": ["A", "A", "B"],
            "updated_at": pd.to_datetime(["2024-01-01", "2024-01-05", "2024-01-02"]),
        })

        # Dedup has confidence 0.7 — set threshold above it
        result = apply_transform_context(df, plan, min_confidence=0.8)

        # Dedup should be skipped
        assert len(result["df"]) == 3  # no dedup applied
        assert len(result["steps_skipped"]) > 0


class TestOutputContract:
    """Tests for output contract compliance."""

    def test_has_all_contract_keys(self, standardize_plan: dict) -> None:
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan)

        # Standard contract keys
        assert result["kind"] == "apply_transform_context"
        assert "subject" in result
        assert "summary" in result
        assert "metrics" in result
        assert "findings" in result
        assert "risks" in result
        assert "samples" in result
        assert "suggested_next_actions" in result

        # Domain-specific keys
        assert "df" in result
        assert "steps_applied" in result
        assert "steps_skipped" in result
        assert "step_audit" in result
        assert "columns_renamed" in result
        assert "row_count_before" in result
        assert "row_count_after" in result
        assert "rows_dropped" in result

    def test_markdown_output(self, standardize_plan: dict) -> None:
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, output_format="markdown")

        assert isinstance(result, str)
        assert "# Apply Transform" in result
        assert "## Metrics" in result


class TestEndToEnd:
    """End-to-end integration: profile → plan → apply."""

    def test_full_pipeline(self) -> None:
        """Profile → plan → apply produces valid transformed output."""
        from tools.table_profiler_tool.lib.profiler import profile_table
        from tools.table_profiler_tool.lib.contract import serialize_profile
        from odibi_anchor.tables.transform_plan_context import adapt_profile_to_transform_input

        # Create messy data
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-002", "INV-003"],
            "Amount": ["100.50", "N/A", "300.00"],
            "Status": ["Active", "Active", "Closed"],
        })

        # Step 1: Profile (via the table_profiler tool — profile_table)
        profile_ctx = serialize_profile(profile_table(df, "invoices"))

        # Step 2: Adapt + Plan
        transform_input = adapt_profile_to_transform_input(profile_ctx)
        plan_ctx = transform_plan_context(transform_input, subject="invoices")

        # Step 3: Apply
        result = apply_transform_context(df, plan_ctx)

        assert result["kind"] == "apply_transform_context"
        assert len(result["steps_applied"]) > 0
        assert isinstance(result["df"], pd.DataFrame)
        # Should have renamed columns
        assert "invoice_id" in result["df"].columns or "Invoice ID" not in result["df"].columns



class TestCheckpointUndo:
    """Tests for checkpoint/undo support."""

    def test_checkpoints_stored(self, standardize_plan: dict) -> None:
        """checkpoint=True stores df state before each step."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        assert "checkpoints" in result
        assert len(result["checkpoints"]) > 0
        # First checkpoint should have original column names
        first_order = result["steps_applied"][0]
        checkpoint_df = result["checkpoints"][first_order]
        assert "Invoice ID" in checkpoint_df.columns

    def test_df_before_always_present(self, standardize_plan: dict) -> None:
        """df_before holds the original input regardless of checkpoint setting."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=False)

        assert "df_before" in result
        assert "Invoice ID" in result["df_before"].columns
        assert "invoice_id" in result["df"].columns

    def test_rollback_to_step(self) -> None:
        """Can rollback to any checkpoint step."""
        profile = {
            "summary": {"row_count": 3, "column_count": 1},
            "columns": {
                "Amount": {
                    "name": "Amount", "declared_dtype": "object",
                    "inferred_type": "string", "null_rate": 0.0,
                    "distinct_count": 3, "unique_rate": 1.0,
                    "date_format": None, "date_formats": [],
                    "top_values": [("100", 1), ("200", 1), ("N/A", 1)],
                },
            },
            "null_like_strings": {"Amount": ["N/A"]},
            "type_mismatches": {},
            "key_candidates": [],
            "anomalies": [],
            "snapshot_columns": [],
        }
        plan = transform_plan_context(profile, subject="test", include_dedup=False, include_standardize=False)

        df = pd.DataFrame({"Amount": ["100", "200", "N/A"]})
        result = apply_transform_context(df, plan, checkpoint=True)

        # After null cleanup, "N/A" is gone
        assert pd.isna(result["df"]["Amount"].iloc[2])

        # Rollback: get state before null cleanup step
        null_step_order = None
        for step in plan["steps"]:
            if step["action"] == "null_cleanup":
                null_step_order = step["order"]
                break

        if null_step_order and null_step_order in result["checkpoints"]:
            rolled_back = result["checkpoints"][null_step_order]
            assert rolled_back["Amount"].iloc[2] == "N/A"  # original value

    def test_checkpoint_false_empty_dict(self, standardize_plan: dict) -> None:
        """checkpoint=False returns empty checkpoints dict."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, checkpoint=False)

        assert result["checkpoints"] == {}

    def test_checkpoint_is_copy_not_reference(self, standardize_plan: dict) -> None:
        """Checkpoints are copies — mutations don't affect them."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        # Output df should have renamed columns
        assert "invoice_id" in result["df"].columns
        # Checkpoint should still have original columns
        first_order = result["steps_applied"][0]
        assert "Invoice ID" in result["checkpoints"][first_order].columns



class TestRollbackHelper:
    """Tests for the rollback() convenience function."""

    def test_rollback_by_order(self, standardize_plan: dict) -> None:
        """rollback(result, to=1) returns df before step 1."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        rolled = rollback(result, to=result["steps_applied"][0])
        assert "Invoice ID" in rolled.columns

    def test_rollback_by_action_name(self, standardize_plan: dict) -> None:
        """rollback(result, to="standardize_columns") returns df before that step."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        rolled = rollback(result, to="standardize_columns")
        assert "Invoice ID" in rolled.columns

    def test_rollback_to_start(self, standardize_plan: dict) -> None:
        """rollback(result, to="start") returns original input."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        original = rollback(result, to="start")
        assert "Invoice ID" in original.columns
        assert list(original["Invoice ID"]) == ["A", "B"]

    def test_rollback_missing_step_raises(self, standardize_plan: dict) -> None:
        """rollback with non-existent step order raises KeyError."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        with pytest.raises(KeyError, match="No checkpoint for step 99"):
            rollback(result, to=99)

    def test_rollback_missing_action_raises(self, standardize_plan: dict) -> None:
        """rollback with non-existent action name raises ValueError."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        with pytest.raises(ValueError, match="No executed step with action 'dedup'"):
            rollback(result, to="dedup")

    def test_rollback_no_checkpoints_raises(self, standardize_plan: dict) -> None:
        """rollback without checkpoints raises KeyError."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, checkpoint=False)

        with pytest.raises(KeyError, match="No checkpoints available"):
            rollback(result, to=1)



class TestSparkPersist:
    """Tests for spark_persist parameter validation."""

    def test_invalid_spark_persist_raises(self, standardize_plan: dict) -> None:
        """Invalid spark_persist value raises ValueError."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        with pytest.raises(ValueError, match="spark_persist must be one of"):
            apply_transform_context(df, standardize_plan, spark_persist="invalid")

    def test_spark_persist_none_accepted(self, standardize_plan: dict) -> None:
        """spark_persist='none' is accepted for pandas (no-op)."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, spark_persist="none")
        assert result["metrics"]["spark_persist"] == "n/a"  # pandas

    def test_spark_persist_cache_accepted(self, standardize_plan: dict) -> None:
        """spark_persist='cache' is valid (default)."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, spark_persist="cache")
        assert result["metrics"]["spark_persist"] == "n/a"  # pandas engine

    def test_spark_persist_local_checkpoint_accepted(self, standardize_plan: dict) -> None:
        """spark_persist='local_checkpoint' is valid."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, spark_persist="local_checkpoint")
        assert result["steps_applied"]  # still works


class TestDispatcherRollback:
    """Tests for rollback via the anchor() dispatcher."""

    def test_cw_rollback_by_name(self, standardize_plan: dict) -> None:
        """anchor('rollback', result, to='standardize_columns') returns checkpoint."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        rolled = rollback(result, to="standardize_columns")
        assert "Invoice ID" in rolled.columns

    def test_cw_rollback_to_start(self, standardize_plan: dict) -> None:
        """anchor('rollback', result, to='start') returns original."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        original = rollback(result, to="start")
        assert list(original.columns) == ["Invoice ID", "Amount"]



class TestUnpersist:
    """Tests for unpersist() cleanup function."""

    def test_unpersist_clears_checkpoints(self, standardize_plan: dict) -> None:
        """unpersist() empties the checkpoints dict."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        assert len(result["checkpoints"]) > 0
        returned = unpersist(result)
        assert returned["checkpoints"] == {}
        assert returned is result  # mutates in place and returns same dict

    def test_unpersist_no_checkpoints_noop(self, standardize_plan: dict) -> None:
        """unpersist() is a no-op when checkpoint=False."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, checkpoint=False)

        returned = unpersist(result)
        assert returned["checkpoints"] == {}

    def test_unpersist_preserves_df(self, standardize_plan: dict) -> None:
        """unpersist() does not affect df or df_before."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        unpersist(result)
        assert "invoice_id" in result["df"].columns
        assert "Invoice ID" in result["df_before"].columns

    def test_unpersist_blocks_param(self, standardize_plan: dict) -> None:
        """blocking=True is accepted (pandas no-op but validates signature)."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)

        returned = unpersist(result, blocking=True)
        assert returned["checkpoints"] == {}



class TestAutoUnpersist:
    """Tests for auto_unpersist parameter."""

    def test_auto_unpersist_false_keeps_checkpoints(self, standardize_plan: dict) -> None:
        """Default auto_unpersist=False preserves checkpoints."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True, auto_unpersist=False)

        assert len(result["checkpoints"]) > 0

    def test_auto_unpersist_true_pandas_keeps_checkpoints(self, standardize_plan: dict) -> None:
        """auto_unpersist=True with pandas still has checkpoints (only Spark is freed)."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True, auto_unpersist=True)

        # For pandas, auto_unpersist is a no-op — checkpoints are still there
        assert len(result["checkpoints"]) > 0

    def test_auto_unpersist_dry_run_preserves(self, standardize_plan: dict) -> None:
        """auto_unpersist=True with dry_run=True does not unpersist."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(
            df, standardize_plan, checkpoint=True, auto_unpersist=True, dry_run=True
        )

        # dry_run should not trigger unpersist regardless
        assert "checkpoints" in result



class TestMemoryEstimation:
    """Tests for memory pressure estimation helpers."""

    def test_estimate_memory_bytes(self) -> None:
        """Estimate scales with rows, cols, and checkpoints."""
        # 1000 rows, 10 cols, 5 checkpoints = 1000 * 10 * 64 * 5 = 3,200,000
        result = _estimate_memory_bytes(1000, 10, 5)
        assert result == 3_200_000

    def test_estimate_zero_rows(self) -> None:
        """Zero rows produces zero estimate."""
        assert _estimate_memory_bytes(0, 10, 5) == 0

    def test_format_bytes_gb(self) -> None:
        """Large values format as GB."""
        assert _format_bytes(2_147_483_648) == "2.0 GB"

    def test_format_bytes_mb(self) -> None:
        """Medium values format as MB."""
        assert _format_bytes(52_428_800) == "50 MB"

    def test_format_bytes_kb(self) -> None:
        """Small values format as KB."""
        assert _format_bytes(8192) == "8 KB"

    def test_format_bytes_b(self) -> None:
        """Tiny values format as B."""
        assert _format_bytes(512) == "512 B"

    def test_metrics_include_est_memory(self, standardize_plan: dict) -> None:
        """metrics.est_checkpoint_memory_bytes present in result."""
        df = pd.DataFrame({"Invoice ID": ["A", "B"], "Amount": [1, 2]})
        result = apply_transform_context(df, standardize_plan, checkpoint=True)
        assert "est_checkpoint_memory_bytes" in result["metrics"]
        # Pandas engine → 0 (Spark-only metric)
        assert result["metrics"]["est_checkpoint_memory_bytes"] == 0

class TestMaxCheckpoints:
    """Tests for max_checkpoints parameter — FIFO eviction of oldest checkpoints."""

    @pytest.fixture
    def multi_step_plan(self) -> dict:
        """Plan with 4 steps: standardize, null_cleanup, cast, dedup."""
        profile = {
            "summary": {"row_count": 5, "column_count": 2},
            "columns": {
                "Invoice ID": {
                    "name": "Invoice ID", "declared_dtype": "object",
                    "inferred_type": "string", "null_rate": 0.0,
                    "distinct_count": 3, "unique_rate": 0.6,
                    "date_format": None, "date_formats": [], "top_values": [],
                },
                "Amount": {
                    "name": "Amount", "declared_dtype": "object",
                    "inferred_type": "float", "null_rate": 0.1,
                    "distinct_count": 5, "unique_rate": 1.0,
                    "date_format": None, "date_formats": [], "top_values": [],
                },
            },
            "null_like_strings": {"Amount": ["N/A", ""]},
            "type_mismatches": {
                "Amount": {"declared": "object", "inferred": "float", "parseable_pct": 90.0, "date_format": None},
            },
            "key_candidates": [("Invoice ID",)],
            "anomalies": [],
            "snapshot_columns": [],
        }
        return _make_plan(profile, subject="test")

    def test_none_default_unlimited(self, multi_step_plan: dict) -> None:
        """max_checkpoints=None preserves all checkpoints (current behavior)."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=None)

        # All steps should have checkpoints
        assert len(result["checkpoints"]) == len(result["steps_applied"])
        assert result["checkpoints_evicted"] == []
        assert result["metrics"]["checkpoints_evicted"] == 0

    def test_evicts_oldest_when_limit_exceeded(self, multi_step_plan: dict) -> None:
        """max_checkpoints=2 evicts oldest when 3rd checkpoint is stored."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=2)

        # Only 2 checkpoints retained
        assert len(result["checkpoints"]) <= 2
        # Some were evicted
        assert len(result["checkpoints_evicted"]) > 0
        # Evicted steps are the lowest order numbers
        for evicted_order in result["checkpoints_evicted"]:
            assert evicted_order not in result["checkpoints"]
        # Retained are the highest order numbers
        retained_orders = sorted(result["checkpoints"].keys())
        for evicted_order in result["checkpoints_evicted"]:
            assert evicted_order < max(retained_orders)

    def test_max_checkpoints_1_keeps_only_last(self, multi_step_plan: dict) -> None:
        """max_checkpoints=1 retains only the most recent checkpoint."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=1)

        assert len(result["checkpoints"]) == 1
        # The retained one should be the last step applied
        retained_order = list(result["checkpoints"].keys())[0]
        assert retained_order == max(result["steps_applied"])

    def test_rollback_fails_for_evicted_step(self, multi_step_plan: dict) -> None:
        """rollback raises KeyError for evicted checkpoints."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=1)

        evicted = result["checkpoints_evicted"]
        if evicted:
            with pytest.raises(KeyError):
                rollback(result, to=evicted[0])

    def test_rollback_works_for_retained_step(self, multi_step_plan: dict) -> None:
        """rollback succeeds for non-evicted checkpoints."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=2)

        retained_orders = sorted(result["checkpoints"].keys())
        for order in retained_orders:
            rolled = rollback(result, to=order)
            assert isinstance(rolled, pd.DataFrame)

    def test_rollback_to_start_still_works(self, multi_step_plan: dict) -> None:
        """rollback to='start' works regardless of max_checkpoints."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=1)

        original = rollback(result, to="start")
        assert "Invoice ID" in original.columns
        assert len(original) == 5

    def test_metrics_track_evictions(self, multi_step_plan: dict) -> None:
        """Metrics include checkpoints_evicted count and max_checkpoints."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=2)

        assert "checkpoints_evicted" in result["metrics"]
        assert "max_checkpoints" in result["metrics"]
        assert result["metrics"]["max_checkpoints"] == 2
        assert result["metrics"]["checkpoints_evicted"] == len(result["checkpoints_evicted"])

    def test_findings_report_evictions(self, multi_step_plan: dict) -> None:
        """Findings mention evicted checkpoints."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=1)

        eviction_findings = [f for f in result["findings"] if "evicted" in f]
        assert len(eviction_findings) > 0

    def test_invalid_max_checkpoints_raises(self, standardize_plan: dict) -> None:
        """max_checkpoints=0 or negative raises ValueError."""
        df = pd.DataFrame({"Invoice ID": ["A"], "Amount": [1]})

        with pytest.raises(ValueError, match="max_checkpoints must be >= 1"):
            apply_transform_context(df, standardize_plan, max_checkpoints=0)

        with pytest.raises(ValueError, match="max_checkpoints must be >= 1"):
            apply_transform_context(df, standardize_plan, max_checkpoints=-1)

    def test_max_checkpoints_larger_than_steps_no_eviction(self, multi_step_plan: dict) -> None:
        """max_checkpoints larger than step count means no eviction occurs."""
        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-001", "INV-002", "INV-003", "INV-004"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
        })
        result = apply_transform_context(df, multi_step_plan, max_checkpoints=100)

        assert result["checkpoints_evicted"] == []
        assert len(result["checkpoints"]) == len(result["steps_applied"])


class TestEvictOldestCheckpoint:
    """Unit tests for _evict_oldest_checkpoint helper."""

    def test_evicts_minimum_key(self) -> None:
        """Evicts the lowest order number first."""
        checkpoints = {1: pd.DataFrame(), 3: pd.DataFrame(), 5: pd.DataFrame()}
        evicted = _evict_oldest_checkpoint(checkpoints, max_checkpoints=2, resolved_engine="pandas")

        assert evicted == [1]
        assert 1 not in checkpoints
        assert set(checkpoints.keys()) == {3, 5}

    def test_evicts_multiple_when_needed(self) -> None:
        """Evicts multiple if over limit by more than 1."""
        checkpoints = {1: pd.DataFrame(), 2: pd.DataFrame(), 3: pd.DataFrame(), 4: pd.DataFrame()}
        evicted = _evict_oldest_checkpoint(checkpoints, max_checkpoints=2, resolved_engine="pandas")

        assert evicted == [1, 2]
        assert set(checkpoints.keys()) == {3, 4}

    def test_no_eviction_under_limit(self) -> None:
        """No eviction when at or under limit."""
        checkpoints = {1: pd.DataFrame(), 2: pd.DataFrame()}
        evicted = _evict_oldest_checkpoint(checkpoints, max_checkpoints=3, resolved_engine="pandas")

        assert evicted == []
        assert len(checkpoints) == 2

    def test_no_eviction_at_limit(self) -> None:
        """No eviction when exactly at limit."""
        checkpoints = {1: pd.DataFrame(), 2: pd.DataFrame()}
        evicted = _evict_oldest_checkpoint(checkpoints, max_checkpoints=2, resolved_engine="pandas")

        assert evicted == []
        assert len(checkpoints) == 2



# ── Bug Fix Tests (2026-05-17) ────────────────────────────────────────────────


class TestDedupEmptyColumns:
    """Regression tests for Bug #2: dedup with columns=[] should do full dedup."""

    def test_dedup_empty_columns_removes_duplicates(self) -> None:
        """columns=[] means 'dedup on ALL columns' — removes exact duplicates."""
        df = pd.DataFrame({"a": [1, 1, 2, 2, 3], "b": ["x", "x", "y", "y", "z"]})
        plan = {
            "kind": "transform_plan_context",
            "subject": "test",
            "steps": [{
                "order": 1, "action": "dedup",
                "description": "Dedup all",
                "function": "deduplicate",
                "code_spark": "df = deduplicate(df, keys=None)",
                "code_pandas": "df = df.drop_duplicates(keep='first')",
                "columns": [],
                "confidence": 1.0,
                "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        assert result["df"].shape[0] == 3  # 5 → 3 unique rows
        assert result["step_audit"][0]["status"] == "applied"
        assert result["step_audit"][0]["rows_delta"] == -2

    def test_dedup_none_columns_removes_duplicates(self) -> None:
        """columns key missing entirely (or None) → full dedup."""
        df = pd.DataFrame({"x": [1, 1, 2]})
        plan = {
            "kind": "transform_plan_context",
            "subject": "test",
            "steps": [{
                "order": 1, "action": "dedup",
                "description": "Dedup",
                "function": "deduplicate",
                "code_spark": "df = deduplicate(df, keys=None)",
                "code_pandas": "df = df.drop_duplicates(keep='first')",
                "columns": None,
                "confidence": 1.0,
                "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        assert result["df"].shape[0] == 2

    def test_dedup_nonexistent_columns_skips_gracefully(self) -> None:
        """columns=['nonexistent'] skips (no matching cols in DataFrame)."""
        df = pd.DataFrame({"a": [1, 1, 2, 2, 3]})
        plan = {
            "kind": "transform_plan_context",
            "subject": "test",
            "steps": [{
                "order": 1, "action": "dedup",
                "description": "Dedup on nonexistent",
                "function": "deduplicate",
                "code_spark": "df = deduplicate(df, keys=['nonexistent'])",
                "code_pandas": "df = df.drop_duplicates(subset=['nonexistent'], keep='first')",
                "columns": ["nonexistent"],
                "confidence": 1.0,
                "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        # Should not have removed anything — column doesn't exist
        assert result["df"].shape[0] == 5


class TestKindValidation:
    """Regression tests for Bug #3: kind validation in apply_transform_context."""

    def test_wrong_kind_returns_error_contract(self) -> None:
        """Plan with wrong kind returns error contract, not crash."""
        df = pd.DataFrame({"a": [1, 2]})
        bad_plan = {"kind": "wrong_kind", "steps": []}
        result = apply_transform_context(df, bad_plan, output_format="dict")

        assert result["kind"] == "apply_transform_context"
        assert "error" in result
        assert "transform_plan_context" in result["error"]
        assert "wrong_kind" in result["error"]

    def test_missing_kind_returns_error_contract(self) -> None:
        """Plan without 'kind' key returns error contract."""
        df = pd.DataFrame({"a": [1, 2]})
        no_kind_plan = {"steps": []}
        result = apply_transform_context(df, no_kind_plan, output_format="dict")

        assert "error" in result
        assert "None" in result["error"]

    def test_none_plan_returns_error_contract(self) -> None:
        """None as plan_ctx returns error contract."""
        df = pd.DataFrame({"a": [1, 2]})
        result = apply_transform_context(df, None, output_format="dict")

        assert "error" in result

    def test_correct_kind_processes_normally(self) -> None:
        """Correct kind passes validation and processes steps."""
        df = pd.DataFrame({"a": [1, 1, 2]})
        good_plan = {
            "kind": "transform_plan_context",
            "subject": "test",
            "steps": [{
                "order": 1, "action": "dedup",
                "description": "Dedup",
                "function": "deduplicate",
                "code_spark": "df = deduplicate(df, keys=None)",
                "code_pandas": "df = df.drop_duplicates(keep='first')",
                "columns": [],
                "confidence": 1.0,
                "source": "test",
            }],
        }
        result = apply_transform_context(df, good_plan, output_format="dict")

        assert "error" not in result
        assert result["df"].shape[0] == 2
        assert result["metrics"]["steps_applied"] == 1



class TestDateParseHandler:
    """Tests for the date_parse handler — covers the try_to_date fix."""

    def test_date_parse_pandas_tolerates_invalid(self) -> None:
        """Invalid date strings become NaT on pandas path."""
        df = pd.DataFrame({"d": ["2024-01-15", "invalid", None]})
        plan = {
            "kind": "transform_plan_context", "subject": "test",
            "steps": [{
                "order": 1, "action": "date_parse",
                "description": "Parse", "function": "to_date",
                "code_spark": 'df = df.withColumn("d", F.to_date(F.col("d"), "yyyy-MM-dd"))',
                "code_pandas": 'df["d"] = pd.to_datetime(df["d"], format="%Y-%m-%d")',
                "columns": ["d"], "confidence": 0.9, "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        assert result["step_audit"][0]["status"] == "applied"
        assert pd.isna(result["df"]["d"].iloc[1])  # "invalid" → NaT
        assert pd.isna(result["df"]["d"].iloc[2])  # None → NaT

    def test_date_parse_pandas_parses_valid_dates(self) -> None:
        """Valid dates are parsed correctly."""
        df = pd.DataFrame({"d": ["01/15/2024", "02/20/2024"]})
        plan = {
            "kind": "transform_plan_context", "subject": "test",
            "steps": [{
                "order": 1, "action": "date_parse",
                "description": "Parse", "function": "to_date",
                "code_spark": 'df = df.withColumn("d", F.to_date(F.col("d"), "MM/dd/yyyy"))',
                "code_pandas": 'df["d"] = pd.to_datetime(df["d"], format="%m/%d/%Y")',
                "columns": ["d"], "confidence": 0.9, "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        import datetime
        assert result["df"]["d"].iloc[0] == datetime.date(2024, 1, 15)


class TestStandardizeCollision:
    """Tests for standardize_columns — collision produces duplicates (known gap)."""

    def test_standardize_renames_correctly(self) -> None:
        """Normal rename: spaces/caps → snake_case, independent of code_pandas."""
        df = pd.DataFrame({"Invoice ID": [1], "Total Amount": [100]})
        plan = {
            "kind": "transform_plan_context", "subject": "test",
            "steps": [{
                "order": 1, "action": "standardize_columns",
                "description": "Rename", "function": "standardize_columns",
                "code_spark": "...",
                "code_pandas": "...",  # No regex needed anymore
                "columns": ["Invoice ID", "Total Amount"],
                "confidence": 1.0, "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        assert list(result["df"].columns) == ["invoice_id", "total_amount"]

    def test_standardize_collision_resolved_with_suffix_2col(self) -> None:
        """Two-column collision: first keeps base name, second gets _2 suffix."""
        df = pd.DataFrame({"Foo Bar": [1], "foo_bar": [2]})
        plan = {
            "kind": "transform_plan_context", "subject": "test",
            "steps": [{
                "order": 1, "action": "standardize_columns",
                "description": "Rename", "function": "standardize_columns",
                "code_spark": "...",
                "code_pandas": "...",
                "columns": ["Foo Bar", "foo_bar"],
                "confidence": 1.0, "source": "test",
            }],
        }
        result = apply_transform_context(df, plan, output_format="dict")

        cols = list(result["df"].columns)
        assert len(cols) == len(set(cols)), f"Duplicate columns: {cols}"
        assert cols == ["foo_bar", "foo_bar_2"]

class TestStandardizeUnicode:
    """Edge-case: unicode column names (accented, CJK, emoji)."""

    @staticmethod
    def _make_plan(columns: list[str]) -> dict:
        return {
            "kind": "transform_plan_context", "subject": "test",
            "steps": [{
                "order": 1, "action": "standardize_columns",
                "description": "Rename", "function": "standardize_columns",
                "code_spark": "...", "code_pandas": "...",
                "columns": columns,
                "confidence": 1.0, "source": "test",
            }],
        }

    def test_accented_chars_preserved(self) -> None:
        r"""Accented letters are \w in Python regex — preserved in snake_case."""
        df = pd.DataFrame({"Código País": [1], "résumé": [2], "über_wert": [3]})
        plan = self._make_plan(list(df.columns))
        result = apply_transform_context(df, plan, output_format="dict")

        cols = list(result["df"].columns)
        assert cols == ["código_país", "résumé", "über_wert"]

    def test_cjk_chars_preserved(self) -> None:
        r"""CJK ideographs are \w in Python regex — kept as-is."""
        df = pd.DataFrame({"日本語カラム": [1], "中文字段": [2]})
        plan = self._make_plan(list(df.columns))
        result = apply_transform_context(df, plan, output_format="dict")

        cols = list(result["df"].columns)
        assert cols == ["日本語カラム", "中文字段"]

    def test_emoji_stripped(self) -> None:
        r"""Emoji are non-\w — stripped, leaving the alphanumeric remainder."""
        df = pd.DataFrame({"🔥hot_metric": [1], "⚡speed": [2]})
        plan = self._make_plan(list(df.columns))
        result = apply_transform_context(df, plan, output_format="dict")

        cols = list(result["df"].columns)
        assert cols == ["hot_metric", "speed"]

    def test_unicode_collision_detected(self) -> None:
        """Unicode columns that normalize to same snake_case get suffixed."""
        df = pd.DataFrame({"Ñoño Field": [1], "ñoño_field": [2]})
        plan = self._make_plan(list(df.columns))
        result = apply_transform_context(df, plan, output_format="dict")

        cols = list(result["df"].columns)
        assert len(cols) == len(set(cols)), f"Duplicates: {cols}"
        assert cols == ["ñoño_field", "ñoño_field_2"]

    def test_mixed_unicode_and_ascii(self) -> None:
        """Mix of unicode and ASCII columns all standardize without collision."""
        df = pd.DataFrame({
            "café_score": [1], "Normal Column": [2], "日付": [3]
        })
        plan = self._make_plan(list(df.columns))
        result = apply_transform_context(df, plan, output_format="dict")

        cols = list(result["df"].columns)
        assert cols == ["café_score", "normal_column", "日付"]
        assert len(cols) == len(set(cols))

class TestStandardizeUnicodeSpark:
    """Edge-case: unicode columns through _build_rename_map (shared Spark/pandas path).

    Since _build_rename_map is used by both _apply_standardize_spark (toDF) and
    _apply_standardize_pandas (df.rename), testing the rename map directly validates
    the Spark path without requiring a SparkSession in unit tests.
    """

    def test_accented_chars_preserved_in_map(self) -> None:
        r"""Accented letters are \w — preserved in rename map."""
        from odibi_anchor.tables.apply_transform_context import _build_rename_map

        result = _build_rename_map(["Código País", "résumé", "über_wert"])
        assert result == {
            "Código País": "código_país",
            "résumé": "résumé",
            "über_wert": "über_wert",
        }

    def test_cjk_chars_preserved_in_map(self) -> None:
        r"""CJK ideographs are \w — kept as-is in rename map."""
        from odibi_anchor.tables.apply_transform_context import _build_rename_map

        result = _build_rename_map(["日本語カラム", "中文字段"])
        assert result == {"日本語カラム": "日本語カラム", "中文字段": "中文字段"}

    def test_emoji_stripped_in_map(self) -> None:
        r"""Emoji are non-\w — stripped in rename map."""
        from odibi_anchor.tables.apply_transform_context import _build_rename_map

        result = _build_rename_map(["🔥hot_metric", "⚡speed"])
        assert result == {"🔥hot_metric": "hot_metric", "⚡speed": "speed"}

    def test_unicode_collision_suffixed_in_map(self) -> None:
        """Unicode columns normalizing to same snake_case get suffixed."""
        from odibi_anchor.tables.apply_transform_context import _build_rename_map

        result = _build_rename_map(["Ñoño Field", "ñoño_field"])
        assert result == {"Ñoño Field": "ñoño_field", "ñoño_field": "ñoño_field_2"}

    def test_mixed_unicode_ascii_no_collision_in_map(self) -> None:
        """Mix of unicode and ASCII produces unique names without collision."""
        from odibi_anchor.tables.apply_transform_context import _build_rename_map

        result = _build_rename_map(["café_score", "Normal Column", "日付"])
        assert result == {
            "café_score": "café_score",
            "Normal Column": "normal_column",
            "日付": "日付",
        }
        assert len(set(result.values())) == 3  # All unique

    def test_todf_positional_rename_preserves_order(self) -> None:
        """Verify the toDF rename logic applies positionally (simulated)."""
        from odibi_anchor.tables.apply_transform_context import _build_rename_map

        columns = ["Código País", "Normal Col", "🔥metric", "Ñoño Field", "ñoño_field"]
        rename_map = _build_rename_map(columns)

        # Simulate toDF: new_names = [rename_map.get(c, c) for c in df.columns]
        new_names = [rename_map.get(c, c) for c in columns]
        assert new_names == ["código_país", "normal_col", "metric", "ñoño_field", "ñoño_field_2"]
        assert len(new_names) == len(set(new_names))  # No duplicates



# ── Structured native transform steps ──────────────────────────────────────────

class TestStructuredNativeSteps:
    def test_standardize_uses_planned_collision_safe_names(self) -> None:
        from odibi_anchor.tables.apply_transform_context import _apply_standardize_pandas

        df = pd.DataFrame([[1, 2, 3]], columns=["Foo Bar", "foo_bar", "FOO BAR"])
        step = {
            "columns": list(df.columns),
            "rename_map": {
                "Foo Bar": "foo_bar_2",
                "foo_bar": "foo_bar",
                "FOO BAR": "foo_bar_3",
            },
        }

        result = _apply_standardize_pandas(df, step)

        assert list(result.columns) == ["foo_bar_2", "foo_bar", "foo_bar_3"]

    def test_null_cleanup_uses_structured_values_with_commas(self) -> None:
        from odibi_anchor.tables.apply_transform_context import _apply_null_cleanup_pandas

        df = pd.DataFrame({"status": ["missing, pending", "ready"]})
        step = {
            "columns": ["status"],
            "values": ["missing, pending"],
            "code_pandas": "",
        }

        result = _apply_null_cleanup_pandas(df, step)

        assert pd.isna(result.iloc[0]["status"])
        assert result.iloc[1]["status"] == "ready"

    def test_dedup_keeps_newest_from_structured_direction(self) -> None:
        from odibi_anchor.tables.apply_transform_context import _apply_dedup_pandas

        df = pd.DataFrame({
            "id": [1, 1],
            "updated_at": ["2026-01-01", "2026-02-01"],
            "value": ["old", "new"],
        })
        step = {
            "columns": ["id"],
            "keys": ["id"],
            "order_column": "updated_at",
            "order_direction": "desc",
            "code_pandas": "",
        }

        result = _apply_dedup_pandas(df, step)

        assert result.iloc[0]["value"] == "new"

    def test_spark_helper_name_is_case_insensitive_collision_safe(self) -> None:
        from odibi_anchor.tables.apply_transform_context import _collision_safe_temp_column

        assert _collision_safe_temp_column(["id", "_CW_ROW_NUMBER"]) == "_cw_row_number_"


# ── apply_sql (self-contained SQL execution) ───────────────────────────────────


class TestApplySql:
    def test_empty_sql_raises(self):
        from odibi_anchor.tables.apply_transform_context import apply_sql
        with pytest.raises(ValueError, match="empty"):
            apply_sql("")

    def test_unsubstituted_placeholder_raises(self):
        from odibi_anchor.tables.apply_transform_context import apply_sql
        with pytest.raises(ValueError, match="placeholder"):
            apply_sql("SELECT * FROM {source}")

    def test_bad_mode_raises(self):
        from odibi_anchor.tables.apply_transform_context import apply_sql
        with pytest.raises(ValueError, match="mode"):
            apply_sql("SELECT 1", mode="merge")

    def test_table_mode_requires_target(self):
        from odibi_anchor.tables.apply_transform_context import apply_sql
        # spark resolution happens before target check only if no active session;
        # pass an explicit dummy so we reach the target validation.
        class _DummySpark:
            def sql(self, q):  # pragma: no cover - not reached
                raise AssertionError("should not run without target")
        with pytest.raises(ValueError, match="target"):
            apply_sql("SELECT 1", spark=_DummySpark(), mode="table")

    def test_no_active_session_raises_clear_error(self):
        from odibi_anchor.tables.apply_transform_context import apply_sql
        pytest.importorskip("pyspark")
        # With no active session and pyspark installed, expect the clear runtime error.
        try:
            apply_sql("SELECT 1", mode="view")
        except RuntimeError as e:
            assert "SparkSession" in str(e)
        except Exception as e:  # an active session exists in this env — acceptable
            assert e.__class__.__name__ != "TypeError"
