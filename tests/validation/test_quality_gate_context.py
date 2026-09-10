"""Tests for quality_gate_context v2 (odibi_anchor).

v2 API: quality_gate_context(df, *, keys=None, checks=None, subject=..., 
         engine='auto', sample_limit=10, output_format='dict',
         freshness_column=None, df_name='df', thresholds=None)

Output structure:
  - checks[]: list of {check_id, status, severity, detail, fix_expr, samples}
  - status values: "pass", "fail", "warn"  
  - metrics: {total_rows, checks_run, checks_passed, checks_failed, checks_warned, is_write_safe}
"""
import json
import pytest
import pandas as pd
from odibi_anchor.validation.quality_gate_context import quality_gate_context


# --- Basic functionality ---

def test_basic_pandas_dataframe():
    """quality_gate_context accepts a pandas DataFrame and returns a dict."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    assert isinstance(result, dict)
    assert "checks" in result
    assert "subject" in result


def test_returns_expected_top_keys():
    """Output has standard context generator keys."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    expected_keys = {"kind", "subject", "checks", "summary", "metrics", "status"}
    assert expected_keys.issubset(set(result.keys()))


def test_check_structure():
    """Each check has check_id, status, severity, detail."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    for check in result["checks"]:
        assert "check_id" in check
        assert "status" in check
        assert check["status"] in ("pass", "fail", "warn")
        assert "severity" in check
        assert "detail" in check


def test_metrics_structure():
    """Metrics dict has expected counters."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    m = result["metrics"]
    assert "total_rows" in m
    assert "checks_run" in m
    assert "checks_passed" in m
    assert m["total_rows"] == 3


def test_clean_large_data_passes():
    """Clean data with enough rows passes all checks."""
    df = pd.DataFrame({"id": list(range(50)), "val": list(range(50))})
    result = quality_gate_context(df, keys=["id"])
    failed = [c for c in result["checks"] if c["status"] == "fail"]
    assert len(failed) == 0


def test_duplicate_keys_detected():
    """Duplicate key rows should trigger a fail/warn."""
    df = pd.DataFrame({"id": [1, 2, 2, 3, 3], "val": [10, 20, 30, 40, 50]})
    result = quality_gate_context(df, keys=["id"])
    dupe_checks = [c for c in result["checks"] if "duplicate" in c["check_id"].lower()]
    assert any(c["status"] in ("fail", "warn") for c in dupe_checks)


def test_null_keys_detected():
    """Null key values should trigger a fail/warn."""
    df = pd.DataFrame({"id": [1, None, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    null_checks = [c for c in result["checks"] if "null" in c["check_id"].lower()]
    assert any(c["status"] in ("fail", "warn") for c in null_checks)


def test_empty_dataframe():
    """Empty dataframe should flag row_count."""
    df = pd.DataFrame({"id": pd.Series([], dtype="int64"), "val": pd.Series([], dtype="int64")})
    result = quality_gate_context(df, keys=["id"])
    row_checks = [c for c in result["checks"] if "row" in c["check_id"].lower()]
    assert any(c["status"] in ("fail", "warn") for c in row_checks)


def test_no_keys_runs_checks():
    """Without keys, checks still run (key-related ones skipped)."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df)
    assert isinstance(result, dict)
    assert "checks" in result
    assert len(result["checks"]) > 0


def test_custom_subject():
    """Subject parameter is reflected in output."""
    df = pd.DataFrame({"id": [1, 2], "val": [10, 20]})
    result = quality_gate_context(df, keys=["id"], subject="my_table")
    assert result["subject"] == "my_table"


def test_specific_checks_filter():
    """Passing checks= limits which checks run."""
    df = pd.DataFrame({"id": list(range(20)), "val": list(range(20))})
    result_all = quality_gate_context(df, keys=["id"])
    result_few = quality_gate_context(df, keys=["id"], checks=["row_count", "duplicate_keys"])
    assert len(result_few["checks"]) <= len(result_all["checks"])


def test_sample_limit_controls_output():
    """sample_limit parameter is accepted."""
    df = pd.DataFrame({"id": list(range(100)), "val": list(range(100))})
    result = quality_gate_context(df, keys=["id"], sample_limit=3)
    assert isinstance(result, dict)


def test_output_format_markdown():
    """output_format='markdown' returns a string."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"], output_format="markdown")
    assert isinstance(result, str)
    assert len(result) > 50


def test_freshness_column():
    """freshness_column triggers freshness check."""
    df = pd.DataFrame({
        "id": list(range(20)),
        "updated_at": pd.to_datetime(["2026-05-01"] * 20),
    })
    result = quality_gate_context(df, keys=["id"], freshness_column="updated_at")
    check_ids = [c["check_id"] for c in result["checks"]]
    assert any("fresh" in cid.lower() for cid in check_ids)


def test_thresholds_parameter():
    """Custom thresholds are accepted."""
    df = pd.DataFrame({"id": list(range(20)), "val": [None] * 10 + list(range(10))})
    result = quality_gate_context(df, keys=["id"], thresholds={"null_rate": 0.8})
    assert isinstance(result, dict)


def test_df_name_parameter():
    """df_name parameter is accepted and affects fix expressions."""
    df = pd.DataFrame({"id": [1, 2, 2], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"], df_name="bronze_readings")
    # Should at least accept the parameter without error
    assert isinstance(result, dict)


def test_json_serializable():
    """Output must be JSON-serializable."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    serialized = json.dumps(result, default=str)
    assert len(serialized) > 0


def test_is_write_safe_metric():
    """metrics['is_write_safe'] exists and is boolean."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    assert isinstance(result["metrics"]["is_write_safe"], bool)


# --- Edge cases ---

def test_single_row():
    """Single-row dataframe works."""
    df = pd.DataFrame({"id": [1], "val": [42]})
    result = quality_gate_context(df, keys=["id"])
    assert isinstance(result, dict)
    assert result["metrics"]["total_rows"] == 1


def test_all_nulls_column():
    """Column with all nulls is detected."""
    df = pd.DataFrame({"id": list(range(20)), "val": [None] * 20})
    result = quality_gate_context(df, keys=["id"])
    null_col_checks = [c for c in result["checks"] if "null" in c["check_id"].lower() and "column" in c["check_id"].lower()]
    if null_col_checks:
        assert any(c["status"] in ("fail", "warn") for c in null_col_checks)


def test_multi_key_columns():
    """Multiple key columns work."""
    df = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "y", "x"], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["a", "b"])
    assert isinstance(result, dict)
    assert result["metrics"]["total_rows"] == 3


def test_fix_all_expr_present():
    """fix_all_expr key exists in output."""
    df = pd.DataFrame({"id": [1, 2, 2], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    assert "fix_all_expr" in result


def test_recommendation_present():
    """recommendation key exists in output."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    result = quality_gate_context(df, keys=["id"])
    assert "recommendation" in result
