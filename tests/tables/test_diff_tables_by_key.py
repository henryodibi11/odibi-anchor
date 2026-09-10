import json
from decimal import Decimal

import pandas as pd
import pytest

from odibi_anchor.tables.diff_ops import diff_tables_by_key


def _finding_types(context):
    return {finding["type"] for finding in context["findings"]}


def _risk_types(context):
    return {risk["type"] for risk in context["risks"]}


def test_identical_tables_return_standard_ok_context():
    old = pd.DataFrame({"id": [1, 2], "status": ["active", "retired"], "mw": [100.0, 50.0]})
    new = old.copy()

    context = diff_tables_by_key(old, new, keys=["id"], subject="asset_snapshot", engine="pandas")

    assert context["kind"] == "diff_tables_by_key"
    assert context["subject"] == "asset_snapshot"
    assert context["status"] == "ok"
    assert {
        "kind",
        "subject",
        "engine",
        "status",
        "summary",
        "metrics",
        "columns",
        "findings",
        "risks",
        "samples",
        "suggested_next_actions",
    } <= set(context)
    assert context["engine"] == "pandas"
    assert context["columns"]["keys"] == ["id"]
    assert context["columns"]["compared"] == ["status", "mw"]
    assert context["metrics"]["old_key_count"] == 2
    assert context["metrics"]["new_key_count"] == 2
    assert context["metrics"]["common_key_count"] == 2
    assert context["metrics"]["added_key_count"] == 0
    assert context["metrics"]["removed_key_count"] == 0
    assert context["metrics"]["changed_key_count"] == 0
    assert context["metrics"]["unchanged_key_count"] == 2
    assert context["metrics"]["compared_column_count"] == 2
    assert context["risks"] == []
    assert context["samples"]["added_keys"] == []
    assert context["samples"]["removed_keys"] == []
    assert context["samples"]["changed_rows"] == []


def test_added_removed_and_changed_rows_are_summarized():
    old = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "status": ["active", "active", "retired"],
            "mw": [100.0, 50.0, 25.0],
        }
    )
    new = pd.DataFrame(
        {
            "id": [2, 3, 4],
            "status": ["active", "active", "new"],
            "mw": [55.0, 25.0, 10.0],
        }
    )

    context = diff_tables_by_key(old, new, keys=["id"], subject="asset_snapshot", engine="pandas")

    assert context["metrics"]["old_key_count"] == 3
    assert context["metrics"]["new_key_count"] == 3
    assert context["metrics"]["common_key_count"] == 2
    assert context["metrics"]["added_key_count"] == 1
    assert context["metrics"]["removed_key_count"] == 1
    assert context["metrics"]["changed_key_count"] == 2
    assert context["metrics"]["unchanged_key_count"] == 0
    assert context["metrics"]["has_changes"] is True
    assert context["samples"]["added_keys"] == [{"id": 4}]
    assert context["samples"]["removed_keys"] == [{"id": 1}]
    assert len(context["samples"]["changed_rows"]) == 2
    assert context["samples"]["changed_rows"][0]["key"] == {"id": 2}
    assert context["samples"]["changed_rows"][0]["changes"] == {"mw": {"old": 50.0, "new": 55.0}}
    assert {item["column"] for item in context["metrics"]["changed_column_counts"]} == {"mw", "status"}
    assert "top_changed_columns" in _finding_types(context)


def test_multi_column_key_comparison():
    old = pd.DataFrame(
        {
            "asset_id": ["A", "A", "B"],
            "snapshot_date": ["2026-01-01", "2026-01-02", "2026-01-01"],
            "status": ["on", "on", "off"],
        }
    )
    new = pd.DataFrame(
        {
            "asset_id": ["A", "A", "B"],
            "snapshot_date": ["2026-01-01", "2026-01-02", "2026-01-01"],
            "status": ["on", "off", "off"],
        }
    )

    context = diff_tables_by_key(old, new, keys=["asset_id", "snapshot_date"], engine="pandas")

    assert context["metrics"]["changed_key_count"] == 1
    assert context["samples"]["changed_rows"] == [
        {
            "key": {"asset_id": "A", "snapshot_date": "2026-01-02"},
            "changed_column_count": 1,
            "changed_columns": ["status"],
            "changes": {"status": {"old": "on", "new": "off"}},
            "truncated_changed_columns": False,
        }
    ]


def test_compare_columns_limits_scope():
    old = pd.DataFrame({"id": [1], "status": ["old"], "amount": [10]})
    new = pd.DataFrame({"id": [1], "status": ["new"], "amount": [10]})

    context = diff_tables_by_key(old, new, keys=["id"], compare_columns=["amount"], engine="pandas")

    assert context["metrics"]["compared_column_count"] == 1
    assert context["metrics"]["changed_key_count"] == 0
    assert context["samples"]["changed_rows"] == []


def test_both_null_values_are_not_changes():
    old = pd.DataFrame({"id": [1, 2], "comment": [None, "same"]})
    new = pd.DataFrame({"id": [1, 2], "comment": [None, "same"]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["metrics"]["changed_key_count"] == 0
    assert context["samples"]["changed_rows"] == []


def test_old_duplicate_keys_block_before_diffing():
    old = pd.DataFrame({"id": [1, 1, 2], "status": ["a", "b", "c"]})
    new = pd.DataFrame({"id": [1, 2], "status": ["a", "c"]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "blocked"
    assert context["metrics"]["old_duplicate_key_count"] == 1
    assert context["metrics"]["old_duplicate_row_count"] == 2
    assert "duplicate_keys_old_df" in _risk_types(context)
    assert context["samples"]["old_duplicate_keys"] == [{"id": 1, "row_count": 2}]
    assert context["samples"]["changed_rows"] == []


def test_new_duplicate_keys_block_before_diffing():
    old = pd.DataFrame({"id": [1, 2], "status": ["a", "c"]})
    new = pd.DataFrame({"id": [1, 2, 2], "status": ["a", "c", "d"]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "blocked"
    assert context["metrics"]["new_duplicate_key_count"] == 1
    assert context["metrics"]["new_duplicate_row_count"] == 2
    assert "duplicate_keys_new_df" in _risk_types(context)
    assert context["samples"]["new_duplicate_keys"] == [{"id": 2, "row_count": 2}]


def test_null_keys_block_comparison():
    old = pd.DataFrame({"id": [1, None], "status": ["a", "bad"]})
    new = pd.DataFrame({"id": [1, 2], "status": ["a", "b"]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "blocked"
    assert context["metrics"]["old_null_key_row_count"] == 1
    assert "null_keys_old_df" in _risk_types(context)
    assert context["samples"]["old_null_key_rows"] == [{"row_index": 1, "id": None}]


def test_key_dtype_changes_block_comparison():
    old = pd.DataFrame({"id": [1, 2], "status": ["a", "b"]})
    new = pd.DataFrame({"id": ["1", "2"], "status": ["a", "b"]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "blocked"
    assert context["metrics"]["key_dtype_change_count"] == 1
    assert "key_dtype_changes" in _risk_types(context)


@pytest.mark.parametrize(
    "old,new,keys,message",
    [
        (pd.DataFrame({"id": [1]}), pd.DataFrame({"other_id": [1]}), ["id"], "new_df is missing"),
        (pd.DataFrame({"other_id": [1]}), pd.DataFrame({"id": [1]}), ["id"], "old_df is missing"),
    ],
)
def test_missing_key_columns_raise(old, new, keys, message):
    with pytest.raises(ValueError, match=message):
        diff_tables_by_key(old, new, keys=keys, engine="pandas")


def test_missing_compare_columns_raise():
    old = pd.DataFrame({"id": [1], "amount": [10]})
    new = pd.DataFrame({"id": [1], "status": ["new"]})

    with pytest.raises(ValueError, match="new_df is missing required compare"):
        diff_tables_by_key(old, new, keys=["id"], compare_columns=["amount"], engine="pandas")


def test_compare_columns_cannot_include_keys():
    old = pd.DataFrame({"id": [1], "amount": [10]})
    new = pd.DataFrame({"id": [1], "amount": [10]})

    with pytest.raises(ValueError, match="compare_columns cannot include key columns"):
        diff_tables_by_key(old, new, keys=["id"], compare_columns=["id"], engine="pandas")


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"keys": []}, "keys must contain at least one column"),
        ({"keys": "id"}, "keys must be a list"),
        ({"keys": ["id", "id"]}, "keys must not contain duplicate"),
        ({"keys": ["id"], "compare_columns": "amount"}, "compare_columns must be a list"),
        ({"keys": ["id"], "compare_columns": ["amount", "amount"]}, "compare_columns must not contain duplicate"),
        ({"keys": ["id"], "sample_limit": -1}, "sample_limit"),
        ({"keys": ["id"], "sample_limit": True}, "sample_limit"),
        ({"keys": ["id"], "engine": "duckdb"}, "Unsupported engine"),
    ],
)
def test_invalid_arguments_raise(kwargs, message):
    old = pd.DataFrame({"id": [1], "amount": [10]})
    new = pd.DataFrame({"id": [1], "amount": [10]})

    with pytest.raises(ValueError, match=message):
        diff_tables_by_key(old, new, **kwargs)


def test_duplicate_dataframe_columns_raise():
    old = pd.DataFrame([[1, "a", "b"]], columns=["id", "amount", "amount"])
    new = pd.DataFrame({"id": [1], "amount": ["a"]})

    with pytest.raises(ValueError, match="old_df contains duplicate column names"):
        diff_tables_by_key(old, new, keys=["id"], engine="pandas")


def test_schema_drift_is_reported_but_common_columns_still_compare():
    old = pd.DataFrame({"id": [1], "status": ["old"], "old_only": ["legacy"]})
    new = pd.DataFrame({"id": [1], "status": ["new"], "new_only": ["future"]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["metrics"]["old_only_column_count"] == 1
    assert context["metrics"]["new_only_column_count"] == 1
    assert context["metrics"]["changed_key_count"] == 1
    assert "columns_only_in_old" in _finding_types(context)
    assert "columns_only_in_new" in _finding_types(context)
    assert "schema_drift" in _risk_types(context)


def test_compare_dtype_changes_are_reported():
    old = pd.DataFrame({"id": [1], "amount": pd.Series([10], dtype="int64")})
    new = pd.DataFrame({"id": [1], "amount": pd.Series([10.0], dtype="float64")})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "ok"
    assert context["metrics"]["compare_dtype_change_count"] == 1
    assert context["metrics"]["changed_key_count"] == 0
    assert "type_changed_columns" in _finding_types(context)
    assert "compare_dtype_changes" in _risk_types(context)


def test_sample_limit_caps_key_and_row_samples():
    old = pd.DataFrame({"id": list(range(5)), "amount": list(range(5))})
    new = pd.DataFrame({"id": list(range(5, 10)), "amount": list(range(5, 10))})

    context = diff_tables_by_key(old, new, keys=["id"], sample_limit=2, engine="pandas")

    assert context["metrics"]["added_key_count"] == 5
    assert context["metrics"]["removed_key_count"] == 5
    assert len(context["samples"]["added_keys"]) == 2
    assert len(context["samples"]["removed_keys"]) == 2
    assert len(context["samples"]["added_rows"]) == 2
    assert len(context["samples"]["removed_rows"]) == 2


def test_sample_limit_zero_returns_empty_samples():
    old = pd.DataFrame({"id": [1], "amount": [10]})
    new = pd.DataFrame({"id": [2], "amount": [20]})

    context = diff_tables_by_key(old, new, keys=["id"], sample_limit=0, engine="pandas")

    assert context["metrics"]["added_key_count"] == 1
    assert context["metrics"]["removed_key_count"] == 1
    assert all(sample == [] for sample in context["samples"].values())


def test_empty_old_marks_all_new_keys_added():
    old = pd.DataFrame(columns=["id", "amount"])
    new = pd.DataFrame({"id": [1, 2], "amount": [10, 20]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "ok"
    assert context["metrics"]["old_key_count"] == 0
    assert context["metrics"]["new_key_count"] == 2
    assert context["metrics"]["added_key_count"] == 2
    assert context["metrics"]["removed_key_count"] == 0
    assert context["metrics"]["changed_key_count"] == 0


def test_empty_new_marks_all_old_keys_removed():
    old = pd.DataFrame({"id": [1, 2], "amount": [10, 20]})
    new = pd.DataFrame(columns=["id", "amount"])

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["status"] == "ok"
    assert context["metrics"]["old_key_count"] == 2
    assert context["metrics"]["new_key_count"] == 0
    assert context["metrics"]["added_key_count"] == 0
    assert context["metrics"]["removed_key_count"] == 2
    assert context["metrics"]["changed_key_count"] == 0


def test_no_compare_columns_only_reports_key_presence():
    old = pd.DataFrame({"id": [1, 2]})
    new = pd.DataFrame({"id": [2, 3]})

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["metrics"]["compared_column_count"] == 0
    assert context["metrics"]["added_key_count"] == 1
    assert context["metrics"]["removed_key_count"] == 1
    assert context["metrics"]["changed_key_count"] == 0
    assert "no_compare_columns" in _finding_types(context)


def test_changed_row_samples_are_json_serializable():
    old = pd.DataFrame(
        {
            "id": [1],
            "as_of": [pd.Timestamp("2026-01-01")],
            "amount": [Decimal("10.50")],
        }
    )
    new = pd.DataFrame(
        {
            "id": [1],
            "as_of": [pd.Timestamp("2026-01-02")],
            "amount": [Decimal("11.75")],
        }
    )

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["metrics"]["changed_key_count"] == 1
    assert context["samples"]["changed_rows"][0]["changes"]["as_of"] == {
        "old": "2026-01-01T00:00:00",
        "new": "2026-01-02T00:00:00",
    }
    json.dumps(context)


def test_wide_table_comparison_adds_low_severity_risk():
    old = pd.DataFrame({"id": [1], **{f"c{i}": [i] for i in range(51)}})
    new = old.copy()

    context = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

    assert context["metrics"]["compared_column_count"] == 51
    assert "wide_table_comparison" in _risk_types(context)


def test_auto_engine_detects_pandas_dataframes():
    old = pd.DataFrame({"id": [1], "amount": [10]})
    new = pd.DataFrame({"id": [1], "amount": [10]})

    context = diff_tables_by_key(old, new, keys=["id"])

    assert context["status"] == "ok"

