"""Tests for odibi_anchor.tables.schema_diff_context."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from odibi_anchor.tables.schema_diff_context import schema_diff_context


REQUIRED_KEYS = {
    "kind",
    "subject",
    "old_subject",
    "new_subject",
    "summary",
    "metrics",
    "added",
    "removed",
    "type_changed",
    "unchanged",
    "reordered",
    "findings",
    "risks",
    "samples",
    "suggested_next_actions",
}


def _columns(items: list[dict]) -> list[str]:
    return [item["column"] for item in items]


def test_no_changes_returns_stable_context_contract() -> None:
    old_df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    new_df = pd.DataFrame({"id": [3, 4], "name": ["c", "d"]})

    ctx = schema_diff_context(old_df, new_df, old_subject="source", new_subject="target")

    assert REQUIRED_KEYS.issubset(ctx.keys())
    assert ctx["kind"] == "schema_diff_context"
    assert ctx["subject"] == "source -> target"
    assert "identical" in ctx["summary"] and "no migration" in ctx["summary"].lower()
    assert ctx["metrics"]["engine"] == "pandas"
    assert ctx["metrics"]["has_changes"] is False
    assert ctx["metrics"]["compatibility"] == "unchanged"
    assert ctx["metrics"]["is_breaking_change"] is False
    assert ctx["added"] == []
    assert ctx["removed"] == []
    assert ctx["type_changed"] == []
    assert _columns(ctx["unchanged"]) == ["id", "name"]
    assert ctx["risks"] == []
    assert ctx["samples"] == {}
    assert any("identical" in a.lower() or "no migration" in a.lower() for a in ctx["suggested_next_actions"])


def test_added_column_is_additive() -> None:
    old_df = pd.DataFrame({"id": [1]})
    new_df = pd.DataFrame({"id": [1], "score": [9.5]})

    ctx = schema_diff_context(old_df, new_df)

    assert _columns(ctx["added"]) == ["score"]
    assert ctx["added"][0]["dtype"] == "float64"
    assert ctx["added"][0]["position"] == 1
    assert ctx["metrics"]["added_count"] == 1
    assert ctx["metrics"]["net_column_delta"] == 1
    assert ctx["metrics"]["compatibility"] == "additive"
    assert ctx["metrics"]["is_breaking_change"] is False
    assert any(finding["check_type"] == "column_added" for finding in ctx["findings"])
    assert ctx["risks"] == []


def test_removed_column_is_breaking() -> None:
    old_df = pd.DataFrame({"id": [1], "score": [9.5]})
    new_df = pd.DataFrame({"id": [1]})

    ctx = schema_diff_context(old_df, new_df)

    assert _columns(ctx["removed"]) == ["score"]
    assert ctx["metrics"]["removed_count"] == 1
    assert ctx["metrics"]["compatibility"] == "breaking"
    assert ctx["metrics"]["is_breaking_change"] is True
    assert any(risk["risk"] == "removed_columns" for risk in ctx["risks"])
    assert any("consumers" in action or "impact" in action for action in ctx["suggested_next_actions"])


def test_type_changed_high_risk_when_type_family_changes() -> None:
    old_df = pd.DataFrame({"id": [1, 2]})
    new_df = pd.DataFrame({"id": ["1", "2"]})

    ctx = schema_diff_context(old_df, new_df)

    assert len(ctx["type_changed"]) == 1
    change = ctx["type_changed"][0]
    assert change["column"] == "id"
    assert change["old_dtype"] == "int64"
    assert change["new_dtype"] == "object"
    assert change["old_type_family"] == "numeric"
    assert change["new_type_family"] == "string"
    assert change["risk_level"] == "high"
    assert ctx["metrics"]["compatibility"] == "breaking"
    assert any(finding["severity"] == "critical" for finding in ctx["findings"])


def test_type_changed_medium_risk_when_type_family_matches() -> None:
    old_df = pd.DataFrame({"amount": pd.Series([1, 2], dtype="int64")})
    new_df = pd.DataFrame({"amount": pd.Series([1.1, 2.2], dtype="float64")})

    ctx = schema_diff_context(old_df, new_df)

    change = ctx["type_changed"][0]
    assert change["old_type_family"] == "numeric"
    assert change["new_type_family"] == "numeric"
    assert change["risk_level"] == "medium"
    assert any(finding["severity"] == "warning" for finding in ctx["findings"])


def test_mixed_schema_changes_are_summarized() -> None:
    old_df = pd.DataFrame({
        "id": pd.Series([1], dtype="int64"),
        "amount": pd.Series([10.0], dtype="float64"),
        "drop_me": ["x"],
    })
    new_df = pd.DataFrame({
        "id": pd.Series(["1"], dtype="object"),
        "amount": pd.Series([10.0], dtype="float64"),
        "new_col": [True],
    })

    ctx = schema_diff_context(old_df, new_df)

    assert ctx["metrics"]["added_count"] == 1
    assert ctx["metrics"]["removed_count"] == 1
    assert ctx["metrics"]["type_changed_count"] == 1
    assert ctx["metrics"]["unchanged_count"] == 1
    assert "1 added" in ctx["summary"] and "1 removed" in ctx["summary"]
    assert ctx["metrics"]["compatibility"] == "breaking"


def test_reordered_columns_are_detected_without_type_change() -> None:
    old_df = pd.DataFrame({"id": [1], "name": ["a"], "amount": [10.0]})
    new_df = pd.DataFrame({"amount": [10.0], "id": [1], "name": ["a"]})

    ctx = schema_diff_context(old_df, new_df)

    assert ctx["metrics"]["reordered_count"] == 3
    assert _columns(ctx["reordered"]) == ["id", "name", "amount"]
    assert ctx["metrics"]["compatibility"] == "unchanged"
    assert ctx["metrics"]["is_breaking_change"] is False
    assert any(risk["risk"] == "column_order_changed" for risk in ctx["risks"])


def test_include_unchanged_false_keeps_count_but_omits_details() -> None:
    old_df = pd.DataFrame({"a": [1], "b": [2], "c": [3]})
    new_df = old_df.copy()

    ctx = schema_diff_context(old_df, new_df, include_unchanged=False)

    assert ctx["unchanged"] == []
    assert ctx["metrics"]["unchanged_count"] == 3
    assert ctx["metrics"]["unchanged_returned_count"] == 0
    assert ctx["metrics"]["unchanged_truncated_count"] == 3


def test_max_unchanged_caps_payload() -> None:
    old_df = pd.DataFrame({f"col_{i}": [i] for i in range(5)})
    new_df = old_df.copy()

    ctx = schema_diff_context(old_df, new_df, max_unchanged=2)

    assert _columns(ctx["unchanged"]) == ["col_0", "col_1"]
    assert ctx["metrics"]["unchanged_count"] == 5
    assert ctx["metrics"]["unchanged_returned_count"] == 2
    assert ctx["metrics"]["unchanged_truncated_count"] == 3


def test_empty_dataframe_with_schema_works() -> None:
    old_df = pd.DataFrame({"id": pd.Series([], dtype="int64")})
    new_df = pd.DataFrame({"id": pd.Series([], dtype="object")})

    ctx = schema_diff_context(old_df, new_df)

    assert ctx["metrics"]["old_column_count"] == 1
    assert ctx["metrics"]["new_column_count"] == 1
    assert ctx["metrics"]["type_changed_count"] == 1
    assert ctx["type_changed"][0]["old_dtype"] == "int64"
    assert ctx["type_changed"][0]["new_dtype"] == "object"


def test_zero_column_dataframes_work() -> None:
    old_df = pd.DataFrame()
    new_df = pd.DataFrame()

    ctx = schema_diff_context(old_df, new_df)

    assert "identical" in ctx["summary"] and "no migration" in ctx["summary"].lower()
    assert ctx["metrics"]["old_column_count"] == 0
    assert ctx["metrics"]["new_column_count"] == 0
    assert ctx["metrics"]["has_changes"] is False


def test_engine_auto_detects_pandas() -> None:
    old_df = pd.DataFrame({"id": [1]})
    new_df = pd.DataFrame({"id": [2]})

    ctx = schema_diff_context(old_df, new_df, engine="auto")

    assert ctx["metrics"]["engine"] == "pandas"


def test_engine_pandas_requires_pandas_inputs() -> None:
    with pytest.raises(TypeError, match="pandas.DataFrame"):
        schema_diff_context({"id": "int"}, {"id": "int"}, engine="pandas")


def test_invalid_engine_raises() -> None:
    old_df = pd.DataFrame({"id": [1]})
    new_df = pd.DataFrame({"id": [1]})

    with pytest.raises(ValueError, match="engine must be"):
        schema_diff_context(old_df, new_df, engine="duckdb")  # type: ignore[arg-type]


def test_negative_max_unchanged_raises() -> None:
    old_df = pd.DataFrame({"id": [1]})
    new_df = pd.DataFrame({"id": [1]})

    with pytest.raises(ValueError, match="max_unchanged"):
        schema_diff_context(old_df, new_df, max_unchanged=-1)


def test_duplicate_pandas_columns_emit_warning_risk() -> None:
    old_df = pd.DataFrame([[1, 2]], columns=["id", "id"])
    new_df = pd.DataFrame([[1, 2]], columns=["id", "id"])

    ctx = schema_diff_context(old_df, new_df)

    assert any(finding["check_type"] == "duplicate_columns" for finding in ctx["findings"])
    assert any(risk["risk"] == "duplicate_column_labels" for risk in ctx["risks"])
    assert any("duplicate column" in action.lower() for action in ctx["suggested_next_actions"])


def test_case_change_is_treated_as_added_and_removed() -> None:
    old_df = pd.DataFrame({"CustomerID": [1]})
    new_df = pd.DataFrame({"customerid": [1]})

    ctx = schema_diff_context(old_df, new_df)

    assert _columns(ctx["removed"]) == ["CustomerID"]
    assert _columns(ctx["added"]) == ["customerid"]
    assert ctx["metrics"]["compatibility"] == "breaking"


def test_boolean_and_datetime_dtypes_are_reported() -> None:
    old_df = pd.DataFrame({
        "is_active": pd.Series([True, False], dtype="bool"),
        "event_ts": pd.to_datetime(["2024-01-01", "2024-01-02"]),
    })
    new_df = pd.DataFrame({
        "is_active": pd.Series(["true", "false"], dtype="object"),
        "event_ts": ["2024-01-01", "2024-01-02"],
    })

    ctx = schema_diff_context(old_df, new_df)

    families = {change["column"]: (change["old_type_family"], change["new_type_family"]) for change in ctx["type_changed"]}
    assert families["is_active"] == ("boolean", "string")
    assert families["event_ts"] == ("datetime", "string")


def test_subject_override() -> None:
    old_df = pd.DataFrame({"id": [1]})
    new_df = pd.DataFrame({"id": [1]})

    ctx = schema_diff_context(old_df, new_df, subject="catalog.schema.table")

    assert ctx["subject"] == "catalog.schema.table"


def test_context_is_json_serializable() -> None:
    old_df = pd.DataFrame({"id": [1], "amount": [10.0]})
    new_df = pd.DataFrame({"id": ["1"], "status": ["new"]})

    ctx = schema_diff_context(old_df, new_df)

    dumped = json.dumps(ctx, sort_keys=True)
    assert "schema_diff_context" in dumped
