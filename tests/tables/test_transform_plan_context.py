"""Tests for odibi_anchor.tables.transform_plan_context."""

from __future__ import annotations

import pytest
import pandas as pd

from odibi_anchor.tables.transform_plan_context import (
    transform_plan_context,
    render_transform_plan_report,
)


def _profile(df, subject="test"):
    """Profile a DataFrame via the table_profiler tool, returning the serialized
    contract dict (kind="profile_table"). Replaces the removed
    dataset_profile_context/exploration_context for these integration tests.
    """
    from tools.table_profiler_tool.lib.profiler import profile_table
    from tools.table_profiler_tool.lib.contract import serialize_profile
    return serialize_profile(profile_table(df, subject))


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def empty_profile() -> dict:
    """Profile with no columns — produces zero steps."""
    return {
        "summary": {"row_count": 0, "column_count": 0},
        "columns": {},
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }


@pytest.fixture
def full_profile() -> dict:
    """Profile exercising all transform rule categories."""
    return {
        "summary": {"row_count": 1000, "column_count": 6},
        "columns": {
            "Invoice ID": {
                "name": "Invoice ID",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.0,
                "distinct_count": 1000,
                "unique_rate": 1.0,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
            "Amount": {
                "name": "Amount",
                "declared_dtype": "object",
                "inferred_type": "float",
                "null_rate": 0.02,
                "distinct_count": 500,
                "unique_rate": 0.5,
                "date_format": None,
                "date_formats": [],
                "top_values": [("100.5", 10), ("200.0", 8), ("N/A", 5)],
            },
            "Created Date": {
                "name": "Created Date",
                "declared_dtype": "object",
                "inferred_type": "date",
                "null_rate": 0.01,
                "distinct_count": 300,
                "unique_rate": 0.3,
                "date_format": "%m/%d/%Y",
                "date_formats": [("%m/%d/%Y", 0.97)],
                "top_values": [],
            },
            "status": {
                "name": "status",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.0,
                "distinct_count": 3,
                "unique_rate": 0.003,
                "date_format": None,
                "date_formats": [],
                "top_values": [("Active", 500), ("Pending", 300), ("Closed", 200)],
            },
            "updated_at": {
                "name": "updated_at",
                "declared_dtype": "datetime64[ns]",
                "inferred_type": "date",
                "null_rate": 0.0,
                "distinct_count": 900,
                "unique_rate": 0.9,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
            "Notes": {
                "name": "Notes",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 1.0,
                "distinct_count": 0,
                "unique_rate": 0.0,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {
            "Amount": ["N/A", ""],
        },
        "type_mismatches": {
            "Amount": {
                "declared": "object",
                "inferred": "float",
                "parseable_pct": 98.5,
                "date_format": None,
            },
            "Created Date": {
                "declared": "object",
                "inferred": "date",
                "parseable_pct": 97.0,
                "date_format": "%m/%d/%Y",
            },
        },
        "key_candidates": [("Invoice ID",)],
        "anomalies": [
            "'Notes' is entirely null",
        ],
        "snapshot_columns": [],
    }


# ── Test Cases ────────────────────────────────────────────────────────────────


def test_empty_profile(empty_profile: dict) -> None:
    """No columns, no steps, summary says '0 transforms'."""
    ctx = transform_plan_context(empty_profile, subject="empty_table")

    assert ctx["kind"] == "transform_plan_context"
    assert ctx["subject"] == "empty_table"
    assert ctx["metrics"]["total_steps"] == 0
    assert ctx["steps"] == []
    assert "0 transforms" in ctx["summary"]


def test_null_cleanup(empty_profile: dict) -> None:
    """One column with null-like strings generates a cleanup step."""
    profile = {
        **empty_profile,
        "columns": {
            "amount": {
                "name": "amount",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.05,
                "distinct_count": 100,
                "unique_rate": 0.1,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {"amount": ["N/A", "None", "", "missing, pending"]},
    }

    ctx = transform_plan_context(profile, subject="test_table", include_dedup=False)

    null_steps = [s for s in ctx["steps"] if s["action"] == "null_cleanup"]
    assert len(null_steps) == 1
    assert null_steps[0]["columns"] == ["amount"]
    assert null_steps[0]["confidence"] == 1.0
    assert null_steps[0]["values"] == ["N/A", "None", "", "missing, pending"]
    assert "F.when" in null_steps[0]["code_spark"]
    assert ".isin(" in null_steps[0]["code_spark"]


def test_cast_float() -> None:
    """Type mismatch with parseable_pct=98.5 generates a cast step."""
    profile = {
        "summary": {"row_count": 100, "column_count": 1},
        "columns": {
            "price": {
                "name": "price",
                "declared_dtype": "object",
                "inferred_type": "float",
                "null_rate": 0.0,
                "distinct_count": 50,
                "unique_rate": 0.5,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "price": {
                "declared": "object",
                "inferred": "float",
                "parseable_pct": 98.5,
                "date_format": None,
            },
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test")

    cast_steps = [s for s in ctx["steps"] if s["action"] == "cast"]
    assert len(cast_steps) == 1
    assert cast_steps[0]["confidence"] == 0.985
    assert "double" in cast_steps[0]["code_spark"]
    assert "to_numeric" in cast_steps[0]["code_pandas"]


def test_cast_skipped_low_parseable() -> None:
    """Parseable_pct=49 → no cast generated."""
    profile = {
        "summary": {"row_count": 100, "column_count": 1},
        "columns": {
            "value": {
                "name": "value",
                "declared_dtype": "object",
                "inferred_type": "float",
                "null_rate": 0.0,
                "distinct_count": 50,
                "unique_rate": 0.5,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "value": {
                "declared": "object",
                "inferred": "float",
                "parseable_pct": 49.0,
                "date_format": None,
            },
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test")

    cast_steps = [s for s in ctx["steps"] if s["action"] == "cast"]
    assert len(cast_steps) == 0


def test_date_parse() -> None:
    """Date type_mismatch with format generates to_date step."""
    profile = {
        "summary": {"row_count": 100, "column_count": 1},
        "columns": {
            "start_date": {
                "name": "start_date",
                "declared_dtype": "object",
                "inferred_type": "date",
                "null_rate": 0.0,
                "distinct_count": 50,
                "unique_rate": 0.5,
                "date_format": "%m/%d/%Y",
                "date_formats": [("%m/%d/%Y", 0.95)],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "start_date": {
                "declared": "object",
                "inferred": "date",
                "parseable_pct": 95.0,
                "date_format": "%m/%d/%Y",
            },
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test")

    date_steps = [s for s in ctx["steps"] if s["action"] == "date_parse"]
    assert len(date_steps) == 1
    assert "MM/dd/yyyy" in date_steps[0]["code_spark"]
    assert "%m/%d/%Y" in date_steps[0]["code_pandas"]
    assert date_steps[0]["confidence"] == 0.95


def test_boolean_cast() -> None:
    """Boolean inferred type generates a native Spark mapping step."""
    profile = {
        "summary": {"row_count": 100, "column_count": 1},
        "columns": {
            "is_active": {
                "name": "is_active",
                "declared_dtype": "object",
                "inferred_type": "boolean",
                "null_rate": 0.0,
                "distinct_count": 2,
                "unique_rate": 0.02,
                "date_format": None,
                "date_formats": [],
                "top_values": [("yes", 60), ("no", 40)],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "is_active": {
                "declared": "object",
                "inferred": "boolean",
                "parseable_pct": 100.0,
                "date_format": None,
            },
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test")

    bool_steps = [s for s in ctx["steps"] if s["action"] == "boolean_cast"]
    assert len(bool_steps) == 1
    assert bool_steps[0]["confidence"] == 0.85
    assert "F.lower" in bool_steps[0]["code_spark"]
    assert "F.when" in bool_steps[0]["code_spark"]
    assert "True" in bool_steps[0]["code_spark"]
    assert "False" in bool_steps[0]["code_spark"]


def test_standardize_columns() -> None:
    """Non-snake-case column names generate standardize step."""
    profile = {
        "summary": {"row_count": 100, "column_count": 2},
        "columns": {
            "Invoice ID": {
                "name": "Invoice ID",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.0,
                "distinct_count": 100,
                "unique_rate": 1.0,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
            "Created Date": {
                "name": "Created Date",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.0,
                "distinct_count": 50,
                "unique_rate": 0.5,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test", include_dedup=False)

    std_steps = [s for s in ctx["steps"] if s["action"] == "standardize_columns"]
    assert len(std_steps) == 1
    assert "Invoice ID" in std_steps[0]["columns"]
    assert "Created Date" in std_steps[0]["columns"]
    assert std_steps[0]["confidence"] == 1.0
    assert "df.toDF" in std_steps[0]["code_spark"]
    assert "invoice_id" in std_steps[0]["output_columns"]


def test_standardize_columns_resolves_collisions_and_literal_names() -> None:
    """Generated and applied paths share safe positional output names."""
    profile = {
        "summary": {"row_count": 1, "column_count": 5},
        "columns": {
            "Foo Bar": {},
            "foo_bar": {},
            "FOO BAR": {},
            "source.name": {},
            'quoted"name': {},
        },
    }

    ctx = transform_plan_context(profile, subject="test")
    step = next(s for s in ctx["steps"] if s["action"] == "standardize_columns")

    assert step["output_columns"] == [
        "foo_bar_2", "foo_bar", "foo_bar_3", "source_name", "quoted_name",
    ]
    assert len(step["output_columns"]) == len(set(step["output_columns"]))
    assert "F.col" not in step["code_spark"]
    compile(step["code_spark"], "<standardize_spark>", "exec")


def test_standardize_columns_reserves_skipped_name() -> None:
    profile = {
        "summary": {"row_count": 1, "column_count": 2},
        "columns": {"Foo Bar": {}, "FOO_BAR": {}},
    }

    ctx = transform_plan_context(
        profile,
        subject="test",
        custom_overrides={"FOO_BAR": "skip"},
    )
    step = next(s for s in ctx["steps"] if s["action"] == "standardize_columns")

    assert step["output_columns"] == ["foo_bar_2", "FOO_BAR"]


def test_execution_order(full_profile: dict) -> None:
    """Steps come out in correct order: standardize, null_clean, cast, date, dedup."""
    ctx = transform_plan_context(full_profile, subject="test")

    actions = [s["action"] for s in ctx["steps"]]

    # Verify ordering constraints
    action_order = {
        "standardize_columns": 0,
        "null_cleanup": 1,
        "cast": 2,
        "date_parse": 3,
        "drop_constant": 4,
        "dedup": 5,
    }

    for i, action in enumerate(actions):
        for j in range(i + 1, len(actions)):
            later_action = actions[j]
            # The later action's category should be >= current action's category
            assert action_order.get(action, 99) <= action_order.get(later_action, 99), (
                f"Step order violated: {action} (order {action_order.get(action)}) "
                f"appeared before {later_action} (order {action_order.get(later_action)})"
            )


def test_dedup_with_snapshot() -> None:
    """Snapshot columns present → dedup keys include snapshot, order_by snapshot desc."""
    profile = {
        "summary": {"row_count": 100, "column_count": 2},
        "columns": {
            "project_id": {
                "name": "project_id",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.0,
                "distinct_count": 50,
                "unique_rate": 0.5,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
            "snapshot_date": {
                "name": "snapshot_date",
                "declared_dtype": "datetime64[ns]",
                "inferred_type": "date",
                "null_rate": 0.0,
                "distinct_count": 10,
                "unique_rate": 0.1,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [("project_id",)],
        "anomalies": [],
        "snapshot_columns": ["snapshot_date"],
    }

    ctx = transform_plan_context(profile, subject="test")

    dedup_steps = [s for s in ctx["steps"] if s["action"] == "dedup"]
    assert len(dedup_steps) == 1
    assert "snapshot_date" in dedup_steps[0]["columns"]
    assert '.orderBy(F.col("snapshot_date").desc())' in dedup_steps[0]["code_spark"]
    assert dedup_steps[0]["order_column"] == "snapshot_date"
    assert dedup_steps[0]["order_direction"] == "desc"
    assert "while _cw_temp.casefold() in _cw_used" in dedup_steps[0]["code_spark"]
    assert dedup_steps[0]["confidence"] == 0.6


def test_custom_overrides_skip() -> None:
    """Override column with 'skip' → no step generated for it."""
    profile = {
        "summary": {"row_count": 100, "column_count": 2},
        "columns": {
            "amount": {
                "name": "amount",
                "declared_dtype": "object",
                "inferred_type": "float",
                "null_rate": 0.0,
                "distinct_count": 50,
                "unique_rate": 0.5,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
            "price": {
                "name": "price",
                "declared_dtype": "object",
                "inferred_type": "float",
                "null_rate": 0.0,
                "distinct_count": 30,
                "unique_rate": 0.3,
                "date_format": None,
                "date_formats": [],
                "top_values": [],
            },
        },
        "null_like_strings": {},
        "type_mismatches": {
            "amount": {"declared": "object", "inferred": "float", "parseable_pct": 99.0, "date_format": None},
            "price": {"declared": "object", "inferred": "float", "parseable_pct": 98.0, "date_format": None},
        },
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test", custom_overrides={"amount": "skip"})

    cast_steps = [s for s in ctx["steps"] if s["action"] == "cast"]
    assert len(cast_steps) == 1
    assert cast_steps[0]["columns"] == ["price"]


def test_code_spark_compiles(full_profile: dict) -> None:
    """Generated spark code passes compile(..., 'exec')."""
    ctx = transform_plan_context(full_profile, subject="test")
    code = ctx["code_spark"]

    # Should not raise
    compile(code, "<test_spark>", "exec")


def test_code_pandas_compiles(full_profile: dict) -> None:
    """Generated pandas code passes compile(..., 'exec')."""
    ctx = transform_plan_context(full_profile, subject="test")
    code = ctx["code_pandas"]

    # Should not raise
    compile(code, "<test_pandas>", "exec")


def test_output_contract(full_profile: dict) -> None:
    """Output has all standard contract keys."""
    ctx = transform_plan_context(full_profile, subject="test")

    # Standard contract keys
    assert "kind" in ctx
    assert "subject" in ctx
    assert "summary" in ctx
    assert "metrics" in ctx
    assert "findings" in ctx
    assert "risks" in ctx
    assert "samples" in ctx
    assert "suggested_next_actions" in ctx

    # Extra domain keys
    assert "steps" in ctx
    assert "code_spark" in ctx
    assert "code_pandas" in ctx
    assert "layer" in ctx

    # Type checks
    assert isinstance(ctx["kind"], str)
    assert isinstance(ctx["metrics"], dict)
    assert isinstance(ctx["findings"], list)
    assert isinstance(ctx["risks"], list)
    assert isinstance(ctx["steps"], list)
    assert isinstance(ctx["code_spark"], str)
    assert isinstance(ctx["code_pandas"], str)


def test_markdown_output(full_profile: dict) -> None:
    """output_format='markdown' returns string with expected sections."""
    result = transform_plan_context(full_profile, subject="test_table", output_format="markdown")

    assert isinstance(result, str)
    assert "# Transform Plan: test_table" in result
    assert "## Metrics" in result
    assert "## Steps" in result
    assert "## Spark Code" in result
    assert "## Pandas Code" in result
    assert "```python" in result


def test_post_null_cleanup_cast() -> None:
    """Column with null-like strings + numeric top_values → null cleanup THEN cast."""
    profile = {
        "summary": {"row_count": 100, "column_count": 1},
        "columns": {
            "quantity": {
                "name": "quantity",
                "declared_dtype": "object",
                "inferred_type": "string",
                "null_rate": 0.05,
                "distinct_count": 20,
                "unique_rate": 0.2,
                "date_format": None,
                "date_formats": [],
                "top_values": [("10", 30), ("20", 25), ("30", 20), ("N/A", 15), ("", 10)],
            },
        },
        "null_like_strings": {"quantity": ["N/A", ""]},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }

    ctx = transform_plan_context(profile, subject="test", include_dedup=False)

    actions = [s["action"] for s in ctx["steps"]]
    assert "null_cleanup" in actions
    assert "cast" in actions
    # Null cleanup must come before cast
    null_idx = actions.index("null_cleanup")
    cast_idx = actions.index("cast")
    assert null_idx < cast_idx


def test_render_function_callable(full_profile: dict) -> None:
    """render_transform_plan_report produces valid markdown from context dict."""
    ctx = transform_plan_context(full_profile, subject="render_test")
    report = render_transform_plan_report(ctx)

    assert isinstance(report, str)
    assert "# Transform Plan: render_test" in report
    assert len(report) > 100


# ── Integration Tests ─────────────────────────────────────────────────────────


def _adapt_profile_output_to_transform_input(profile_ctx: dict) -> dict:
    """Adapt profiler output -> transform_plan_context input.

    Thin wrapper over the production adapter so these integration tests exercise
    the real adapt_profile_to_transform_input path (profile_table or legacy).
    """
    from odibi_anchor.tables.transform_plan_context import adapt_profile_to_transform_input
    return adapt_profile_to_transform_input(profile_ctx)


class TestIntegrationWithProfiler:
    """Integration tests: dataset_profile_context → transform_plan_context."""

    @pytest.fixture
    def messy_dataframe(self):
        """Realistic messy DataFrame simulating raw bronze data."""
        import pandas as pd

        return pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-002", "INV-003", "INV-004", "INV-005"],
            "Amount": ["100.50", "200.00", "N/A", "350.75", ""],
            "Due Date": ["01/15/2024", "02/20/2024", "03/10/2024", "04/05/2024", "05/01/2024"],
            "Is Paid": ["yes", "no", "yes", "no", "yes"],
            "Notes": [None, None, None, None, None],
            "Data Source": ["SAP", "SAP", "SAP", "SAP", "SAP"],
        })

    def test_profile_to_transform_end_to_end(self, messy_dataframe) -> None:
        """Full pipeline: profile messy data → adapt → generate transform plan."""

        # Step 1: Profile the DataFrame
        profile_ctx = _profile(messy_dataframe, "bronze.invoices")

        assert profile_ctx["kind"] == "profile_table"
        assert profile_ctx["metrics"]["row_count"] == 5

        # Step 2: Adapt profiler output to transform input
        transform_input = _adapt_profile_output_to_transform_input(profile_ctx)

        # Verify adapted structure has all required keys
        assert "columns" in transform_input
        assert "null_like_strings" in transform_input
        assert "type_mismatches" in transform_input
        assert "key_candidates" in transform_input
        assert "anomalies" in transform_input
        assert "snapshot_columns" in transform_input

        # Step 3: Generate transform plan
        plan_ctx = transform_plan_context(
            transform_input,
            subject="bronze.invoices",
        )

        assert plan_ctx["kind"] == "transform_plan_context"
        assert plan_ctx["subject"] == "bronze.invoices"
        assert plan_ctx["metrics"]["total_steps"] > 0

        # Step 4: Verify generated code compiles
        compile(plan_ctx["code_spark"], "<integration_spark>", "exec")
        compile(plan_ctx["code_pandas"], "<integration_pandas>", "exec")

    def test_profile_detects_null_like_strings(self, messy_dataframe) -> None:
        """Profiler detects null-like strings → transform generates cleanup steps."""

        profile_ctx = _profile(messy_dataframe, "test")
        transform_input = _adapt_profile_output_to_transform_input(profile_ctx)

        # The profiler should have detected null-like values in Amount (N/A, empty)
        # and Notes (entirely null)
        plan_ctx = transform_plan_context(transform_input, subject="test")

        # Should have at least one step
        assert len(plan_ctx["steps"]) > 0
        # Generated code should be valid
        compile(plan_ctx["code_spark"], "<test_spark>", "exec")
        compile(plan_ctx["code_pandas"], "<test_pandas>", "exec")

    def test_profile_detects_type_mismatches(self, messy_dataframe) -> None:
        """Profiler detects typed-looking strings → transform generates cast steps."""

        profile_ctx = _profile(messy_dataframe, "test")
        transform_input = _adapt_profile_output_to_transform_input(profile_ctx)

        plan_ctx = transform_plan_context(transform_input, subject="test")

        # The Amount column should trigger type_mismatches → cast step
        # Check that some cast or boolean_cast step exists
        actions = [s["action"] for s in plan_ctx["steps"]]
        has_type_transform = "cast" in actions or "boolean_cast" in actions or "date_parse" in actions
        assert has_type_transform, f"Expected type transform steps, got: {actions}"

    def test_profile_detects_non_snake_case(self, messy_dataframe) -> None:
        """Profiler sees non-snake-case columns → transform generates standardize step."""

        profile_ctx = _profile(messy_dataframe, "test")
        transform_input = _adapt_profile_output_to_transform_input(profile_ctx)

        plan_ctx = transform_plan_context(transform_input, subject="test")

        # "Invoice ID", "Due Date", "Is Paid", "Data Source" → standardize step
        std_steps = [s for s in plan_ctx["steps"] if s["action"] == "standardize_columns"]
        assert len(std_steps) == 1
        assert len(std_steps[0]["columns"]) >= 3  # At least 3 columns need renaming

    def test_profile_constant_column_in_anomalies(self, messy_dataframe) -> None:
        """Constant column detected → appears in anomalies → drop available."""

        profile_ctx = _profile(messy_dataframe, "test")
        transform_input = _adapt_profile_output_to_transform_input(profile_ctx)

        # "Data Source" is constant (all "SAP"), should be in anomalies
        assert any("constant" in a for a in transform_input["anomalies"])

        # With include_drop_constant=True, should generate drop step
        plan_ctx = transform_plan_context(
            transform_input,
            subject="test",
            include_drop_constant=True,
        )

        drop_steps = [s for s in plan_ctx["steps"] if s["action"] == "drop_constant"]
        assert len(drop_steps) >= 1

    def test_markdown_rendering_from_profile(self, messy_dataframe) -> None:
        """Full pipeline produces valid markdown output."""

        profile_ctx = _profile(messy_dataframe, "invoices")
        transform_input = _adapt_profile_output_to_transform_input(profile_ctx)

        result = transform_plan_context(
            transform_input,
            subject="bronze.invoices",
            output_format="markdown",
        )

        assert isinstance(result, str)
        assert "# Transform Plan: bronze.invoices" in result
        assert "## Steps" in result
        assert "```python" in result



class TestAutoDetectProfilerOutput:
    """Tests for auto-detecting profiler output in transform_plan_context."""

    def test_accepts_dataset_profile_context_directly(self) -> None:
        """anchor('transform', anchor('profile', df)) works without manual adapt."""

        df = pd.DataFrame({
            "Invoice ID": ["INV-001", "INV-002", "INV-003"],
            "Amount": ["100.50", "N/A", "300.00"],
            "Status": ["Active", "Active", "Closed"],
        })

        # Get raw profiler output
        profile_ctx = _profile(df, "invoices")
        assert profile_ctx["kind"] == "profile_table"

        # Pass directly to transform — should auto-detect and adapt
        plan = transform_plan_context(profile_ctx, subject="invoices")

        assert plan["kind"] == "transform_plan_context"
        assert plan["metrics"]["total_steps"] > 0
        # Should have standardize step (Invoice ID needs snake_case)
        actions = [s["action"] for s in plan["steps"]]
        assert "standardize_columns" in actions

    def test_accepts_exploration_context_directly(self) -> None:
        """anchor('transform', anchor('explore', df)) works without manual adapt."""

        df = pd.DataFrame({
            "project_id": [1, 2, 3, 4, 5],
            "amount": ["100", "200", "N/A", "400", "500"],
            "updated_at": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]),
        })

        # Get exploration output
        explore_ctx = _profile(df, "projects")
        assert explore_ctx["kind"] == "profile_table"

        # Pass directly to transform
        plan = transform_plan_context(explore_ctx, subject="projects")

        assert plan["kind"] == "transform_plan_context"
        # Code should still compile
        compile(plan["code_spark"], "<auto_detect_spark>", "exec")
        compile(plan["code_pandas"], "<auto_detect_pandas>", "exec")

    def test_plain_dict_still_works(self, full_profile: dict) -> None:
        """Normal transform-format dict still works (no 'kind' key)."""
        plan = transform_plan_context(full_profile, subject="test")

        assert plan["kind"] == "transform_plan_context"
        assert plan["metrics"]["total_steps"] > 0

    def test_auto_detect_preserves_subject(self) -> None:
        """Auto-detect uses subject param, not profiler's subject."""

        df = pd.DataFrame({"Col A": ["x", "y", "z"]})
        profile_ctx = _profile(df, "original_name")

        plan = transform_plan_context(profile_ctx, subject="my_custom_name")

        assert plan["subject"] == "my_custom_name"


# ── SQL-first: per-column transforms + assembled code_sql ──────────────────────

def _tf_profile(columns: dict, **extra) -> dict:
    """Build a transform-format profile dict directly (no profiler dependency)."""
    base = {
        "summary": {"row_count": 100, "column_count": len(columns)},
        "columns": columns,
        "null_like_strings": {},
        "type_mismatches": {},
        "key_candidates": [],
        "anomalies": [],
        "snapshot_columns": [],
    }
    base.update(extra)
    return base


def _col(name, **kw):
    c = {
        "name": name, "declared_dtype": "object", "inferred_type": "string",
        "null_rate": 0.0, "distinct_count": 10, "unique_rate": 0.1,
        "semantic_type": "unknown", "is_constant": False, "is_unique": False,
        "null_like_values": [], "has_leading_spaces": False, "has_trailing_spaces": False,
        "has_mixed_case": False, "has_embedded_units": False, "sentinel_values": [],
        "normalization_gap": 0.0, "top_values": [], "date_format": None, "date_formats": [],
    }
    c.update(kw)
    return c


class TestColumnTransforms:
    def test_currency_uses_try_cast_regexp(self):
        prof = _tf_profile(
            {"amount": _col("amount", inferred_type="float", semantic_type="currency_amount")},
            type_mismatches={"amount": {"declared": "object", "inferred": "float", "parseable_pct": 98.0}},
        )
        ctx = transform_plan_context(prof, subject="t")
        t = next(t for t in ctx["column_transforms"] if t["transform"] == "parse_currency")
        assert "TRY_CAST" in t["sql"] and "REGEXP_REPLACE" in t["sql"]
        assert t["auto_apply"] is True  # 0.98 >= 0.95

    def test_trim_from_whitespace_signal(self):
        prof = _tf_profile({"notes": _col("notes", has_trailing_spaces=True)})
        ctx = transform_plan_context(prof, subject="t")
        t = next(t for t in ctx["column_transforms"] if t["transform"] == "trim")
        assert t["sql"] == "NULLIF(TRIM(`notes`), '') AS `notes`"
        assert t["auto_apply"] is True

    def test_null_cleanup_tokens(self):
        prof = _tf_profile({"x": _col("x", null_like_values=["N/A", "TBD"])})
        ctx = transform_plan_context(prof, subject="t")
        t = next(t for t in ctx["column_transforms"] if t["transform"] == "null_cleanup")
        assert "CASE WHEN TRIM(`x`) IN ('N/A', 'TBD')" in t["sql"]

    def test_sentinel_is_destructive_review(self):
        prof = _tf_profile({"svc": _col("svc", sentinel_values=["-999"])})
        ctx = transform_plan_context(prof, subject="t")
        t = next(t for t in ctx["column_transforms"] if t["transform"] == "map_sentinel")
        assert t["destructive"] is True
        assert t["auto_apply"] is False
        assert "svc" in ctx["needs_review"]


class TestConfidenceGating:
    def test_lossy_below_threshold_goes_to_review(self):
        # date with no format → conf 0.85 < 0.95 → review
        prof = _tf_profile({"d": _col("d", inferred_type="date", semantic_type="date_string")})
        ctx = transform_plan_context(prof, subject="t")
        t = next(t for t in ctx["column_transforms"] if t["transform"] == "date_parse")
        assert t["auto_apply"] is False
        assert "d" in ctx["needs_review"]

    def test_destructive_never_auto_even_at_full_confidence(self):
        prof = _tf_profile({"k": _col("k", is_constant=True)})
        ctx = transform_plan_context(prof, subject="t")
        t = next(t for t in ctx["column_transforms"] if t["transform"] == "drop_constant")
        assert t["confidence"] == 1.0
        assert t["auto_apply"] is False


class TestCodeSqlAssembly:
    def test_projection_includes_all_columns_in_order(self):
        prof = _tf_profile({
            "a": _col("a"),
            "b": _col("b", has_trailing_spaces=True),
            "c": _col("c"),
        })
        ctx = transform_plan_context(prof, subject="t")
        sql = ctx["code_sql"]
        # original order preserved; untouched columns pass through; b is trimmed
        assert sql.index("`a`") < sql.index("`b`") < sql.index("`c`")
        assert "NULLIF(TRIM(`b`)" in sql

    def test_review_items_not_in_main_select(self):
        prof = _tf_profile({"k": _col("k", is_constant=True), "v": _col("v")})
        ctx = transform_plan_context(prof, subject="t")
        sql = ctx["code_sql"]
        select_part = sql.split("-- REVIEW")[0]
        assert "drop_constant" in sql  # surfaced in REVIEW block
        assert "omit `k`" in sql       # the review comment
        assert "`k`" in select_part    # constant column still passes through (not auto-dropped)

    def test_three_level_subject_becomes_source(self):
        prof = _tf_profile({"a": _col("a")})
        ctx = transform_plan_context(prof, subject="cat.sch.tbl")
        assert "FROM cat.sch.tbl" in ctx["code_sql"]

    def test_non_table_subject_leaves_placeholder(self):
        prof = _tf_profile({"a": _col("a")})
        ctx = transform_plan_context(prof, subject="my_df")
        assert "FROM {source}" in ctx["code_sql"]

    def test_ctas_and_view_wrappers(self):
        prof = _tf_profile({"a": _col("a")})
        ctx = transform_plan_context(prof, subject="cat.sch.tbl", target="cat.sch.clean")
        assert ctx["code_sql_ctas"].startswith("CREATE OR REPLACE TABLE cat.sch.clean AS")
        assert "CREATE OR REPLACE TEMP VIEW cat.sch.clean_clean AS" in ctx["code_sql_view"]
