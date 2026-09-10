"""Tests for odibi_anchor.debugging.error_trace_context."""

from __future__ import annotations

import traceback

import pandas as pd
import pytest

from odibi_anchor.debugging import error_trace_context


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _capture_trace(fn) -> tuple[str, BaseException]:
    """Run a function and return formatted traceback text plus the exception."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - test helper intentionally captures arbitrary failures
        return traceback.format_exc(), exc
    raise AssertionError("Expected helper function to raise an exception.")


@pytest.fixture
def project_df() -> pd.DataFrame:
    """Synthetic project-level dataset used by pandas failure tests."""
    return pd.DataFrame(
        {
            "project_id": ["P1", "P2", "P3", "P4"],
            "project_name": ["Solar A", "Wind B", "Battery C", "Solar D"],
            "capacity_mw": [100.0, 250.5, None, 75.2],
            "region": ["ERCOT", "MISO", "CAISO", "PJM"],
        }
    )


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_output_contract_keys_exist(project_df: pd.DataFrame):
    trace, _ = _capture_trace(lambda: project_df["missing_capacity_mw"])

    result = error_trace_context(trace, df=project_df, subject="project_df")

    assert set(result).issuperset(
        {
            "kind",
            "subject",
            "summary",
            "metrics",
            "error",
            "location",
            "trace_frames",
            "dataframe_context",
            "findings",
            "risks",
            "samples",
            "relevant_trace",
            "suggested_next_actions",
        }
    )
    assert result["kind"] == "error_trace_context"
    assert result["subject"] == "project_df"
    assert isinstance(result["summary"], str)
    assert isinstance(result["suggested_next_actions"], list)


# ---------------------------------------------------------------------------
# Python / pandas failures
# ---------------------------------------------------------------------------


def test_key_error_from_pandas_dataframe_extracts_column_context(project_df: pd.DataFrame):
    trace, exc = _capture_trace(lambda: project_df["missing_capacity_mw"])

    result_from_text = error_trace_context(trace, df=project_df, subject="project_df")
    result_from_exception = error_trace_context(exc, df=project_df, subject="project_df")

    assert result_from_text["error"]["type"] == "KeyError"
    assert result_from_text["error"]["category"] == "schema_or_column_reference"
    assert "missing_capacity_mw" in result_from_text["dataframe_context"]["referenced_columns_missing"]
    assert result_from_text["metrics"]["has_dataframe_context"] is True
    assert result_from_exception["error"]["type"] == "KeyError"


def test_value_error_numeric_conversion_is_type_conversion():
    df = pd.DataFrame({"amount": ["10.5", "bad", "25.0"]})
    trace, _ = _capture_trace(lambda: df["amount"].astype(float))

    result = error_trace_context(trace, df=df, subject="amount_cast")

    assert result["error"]["type"] == "ValueError"
    assert result["error"]["category"] == "type_conversion"
    assert any("converted" in action or "cast" in action for action in result["suggested_next_actions"])


def test_pandas_merge_validation_error_is_merge_or_key():
    left = pd.DataFrame({"project_id": ["P1", "P1"], "capacity_mw": [100, 101]})
    right = pd.DataFrame({"project_id": ["P1"], "region": ["ERCOT"]})
    trace, _ = _capture_trace(lambda: left.merge(right, on="project_id", validate="one_to_one"))

    result = error_trace_context(trace, df=left, subject="project_merge")

    assert result["error"]["type"] == "MergeError"
    assert result["error"]["category"] == "merge_or_key"
    assert any("duplicate" in action.lower() or "grain" in action.lower() for action in result["suggested_next_actions"])


def test_attribute_error_unknown_dataframe_method_has_location(project_df: pd.DataFrame):
    trace, _ = _capture_trace(lambda: project_df.not_a_method())

    result = error_trace_context(trace, df=project_df)

    assert result["error"]["type"] == "AttributeError"
    assert result["location"]["failing_line"] is not None
    assert result["metrics"]["trace_frame_count"] >= 1


# ---------------------------------------------------------------------------
# Spark / SQL / Databricks-style text parsing without Spark dependency
# ---------------------------------------------------------------------------


def test_spark_analysis_exception_string_is_column_reference():
    raw = """
AnalysisException: [UNRESOLVED_COLUMN.WITH_SUGGESTION] A column or function parameter with name `meter_id` cannot be resolved. Did you mean one of the following? [`asset_id`, `timestamp`, `mw`].;
'Project [meter_id#12]
+- Relation [asset_id#1,timestamp#2,mw#3] parquet
"""

    result = error_trace_context(raw, subject="spark_select")

    assert result["error"]["type"] == "AnalysisException"
    assert result["error"]["category"] == "schema_or_column_reference"
    assert "meter_id" in result["findings"][2]
    assert result["metrics"]["has_dataframe_context"] is False


def test_sql_parse_exception_extracts_line_and_position():
    raw = """
ParseException: [PARSE_SYNTAX_ERROR] Syntax error at or near 'FROM'.(line 1, pos 7)

== SQL ==
SELECT FROM catalog.schema.table
-------^^^
"""

    result = error_trace_context(raw, subject="generated_sql")

    assert result["error"]["type"] == "ParseException"
    assert result["error"]["category"] == "syntax"
    assert result["location"]["sql_line"] == 1
    assert result["location"]["sql_position"] == 7
    assert any("SQL" in action or "line" in action for action in result["suggested_next_actions"])


def test_databricks_command_frame_is_parsed():
    raw = '''Traceback (most recent call last):
  File "<command-123456789>", line 4, in <module>
    result = transform_projects(df)
  File "/Workspace/Repos/team/project/notebooks/transform.py", line 27, in transform_projects
    return df["missing_col"]
KeyError: 'missing_col'
'''

    result = error_trace_context(raw, subject="databricks_cell")

    assert result["location"]["failing_file"].endswith("transform.py")
    assert result["location"]["failing_line"] == 27
    assert result["location"]["code"] == 'return df["missing_col"]'
    assert result["metrics"]["trace_frame_count"] == 2


# ---------------------------------------------------------------------------
# Edge cases and guardrails
# ---------------------------------------------------------------------------


def test_empty_error_returns_stable_context():
    result = error_trace_context("", subject="empty")

    assert result["error"]["type"] == "UnknownError"
    assert result["error"]["message"] == "No error text provided."
    assert result["metrics"]["input_char_count"] == 0
    assert any("No error text" in risk for risk in result["risks"])


def test_max_chars_truncates_relevant_trace_and_records_risk():
    repeated_frames = "\n".join(
        f'  File "/tmp/example_{idx}.py", line {idx}, in fn_{idx}\n    do_work_{idx}()'
        for idx in range(100)
    )
    raw = f"Traceback (most recent call last):\n{repeated_frames}\nValueError: bad value"

    result = error_trace_context(raw, max_chars=300, sample_limit=100)

    assert len(result["relevant_trace"]) <= 300
    assert any("truncated" in risk.lower() for risk in result["risks"])


def test_sample_limit_controls_trace_frames_and_dataframe_rows(project_df: pd.DataFrame):
    raw = '''Traceback (most recent call last):
  File "/tmp/a.py", line 1, in a
    a()
  File "/tmp/b.py", line 2, in b
    b()
  File "/tmp/c.py", line 3, in c
    c()
KeyError: 'missing_capacity_mw'
'''

    result = error_trace_context(raw, df=project_df, sample_limit=2)

    assert len(result["trace_frames"]) == 2
    assert len(result["dataframe_context"]["sample_rows"]) == 2
    assert len([sample for sample in result["samples"].get("context_rows", []) if sample["sample_type"] == "trace_frame"]) == 2


def test_metadata_is_compacted_and_unapproved_keys_are_dropped():
    result = error_trace_context(
        "ValueError: bad value",
        metadata={
            "job_id": 123,
            "notebook_path": "/Workspace/Users/example/debug",
            "secret_token": "do-not-include",
            "step": "normalize_projects",
        },
    )

    assert result["metadata"] == {
        "job_id": 123,
        "notebook_path": "/Workspace/Users/example/debug",
        "step": "normalize_projects",
    }
    assert result["subject"] == "normalize_projects"


def test_unsupported_dataframe_engine_records_risk():
    class DummyDataFrame:
        pass

    result = error_trace_context("ValueError: bad value", df=DummyDataFrame())

    assert result["dataframe_context"] is None
    assert any("unsupported engine" in risk for risk in result["risks"])


def test_spark_dataframe_placeholder_records_risk():
    class FakeSparkDataFrame:
        columns = ["id"]
        schema = "struct<id:int>"

        def select(self, *args):
            return self

    result = error_trace_context("AnalysisException: bad spark thing", df=FakeSparkDataFrame())

    assert result["dataframe_context"] is None
    assert any("Spark DataFrame context was not collected" in risk for risk in result["risks"])


def test_exception_chain_uses_final_exception():
    raw = '''Traceback (most recent call last):
ValueError: invalid source value

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
RuntimeError: pipeline step failed
'''

    result = error_trace_context(raw)

    assert result["error"]["type"] == "RuntimeError"
    assert result["error"]["message"] == "pipeline step failed"
    assert result["metrics"]["exception_chain_count"] == 2


def test_invalid_limits_raise_helpful_errors():
    with pytest.raises(ValueError, match="max_chars"):
        error_trace_context("ValueError: x", max_chars=100)

    with pytest.raises(ValueError, match="sample_limit"):
        error_trace_context("ValueError: x", sample_limit=-1)
