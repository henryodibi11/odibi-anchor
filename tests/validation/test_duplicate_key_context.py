import json

import numpy as np
import pandas as pd
import pytest

from odibi_anchor.validation.duplicate_key_context import duplicate_key_context


EXPECTED_TOP_LEVEL_KEYS = {
    "kind",
    "subject",
    "summary",
    "passed",
    "has_duplicates",
    "has_null_keys",
    "metrics",
    "findings",
    "risks",
    "samples",
    "suggested_next_actions",
    "duplicate_pattern",
}


def test_no_duplicates_returns_pass_context():
    df = pd.DataFrame({
        "order_id": [1, 2, 3],
        "amount": [100.0, 200.0, 300.0],
    })

    result = duplicate_key_context(df, keys=["order_id"], subject="orders")

    assert set(result) == EXPECTED_TOP_LEVEL_KEYS
    assert result["kind"] == "duplicate_key_context"
    assert result["subject"] == "orders"
    assert result["passed"] is True
    assert result["has_duplicates"] is False
    assert result["has_null_keys"] is False
    assert result["metrics"]["row_count"] == 3
    assert result["metrics"]["unique_key_count"] == 3
    assert result["metrics"]["duplicate_key_count"] == 0
    assert result["metrics"]["duplicate_row_count"] == 0
    assert result["metrics"]["excess_duplicate_row_count"] == 0
    assert result["metrics"]["duplicate_key_rate"] == 0.0
    assert result["metrics"]["duplicate_row_rate"] == 0.0
    assert result["samples"] == {}
    assert result["risks"] == []
    assert "Proceed" in result["suggested_next_actions"][0]


def test_single_key_duplicates_returns_counts_rates_and_samples():
    df = pd.DataFrame({
        "order_id": [1, 1, 2, 3, 3, 3],
        "amount": [100, 110, 200, 300, 310, 320],
    })

    result = duplicate_key_context(df, keys="order_id", subject="orders")

    assert result["passed"] is False
    assert result["has_duplicates"] is True
    assert result["metrics"]["row_count"] == 6
    assert result["metrics"]["unique_key_count"] == 3
    assert result["metrics"]["duplicate_key_count"] == 2
    assert result["metrics"]["duplicate_row_count"] == 5
    assert result["metrics"]["excess_duplicate_row_count"] == 3
    assert result["metrics"]["duplicate_key_rate"] == pytest.approx(0.666667)
    assert result["metrics"]["duplicate_row_rate"] == pytest.approx(0.833333)
    assert result["metrics"]["excess_duplicate_row_rate"] == pytest.approx(0.5)
    assert result["metrics"]["max_rows_per_key"] == 3
    assert result["samples"]["duplicate_keys"] == [
        {"key": {"order_id": 3}, "row_count": 3, "excess_row_count": 2},
        {"key": {"order_id": 1}, "row_count": 2, "excess_row_count": 1},
    ]
    assert any("intended business grain" in risk for risk in result["risks"])


def test_composite_key_duplicates():
    df = pd.DataFrame({
        "project_id": ["A", "A", "A", "B", "B"],
        "snapshot_date": ["2026-01-01", "2026-01-01", "2026-02-01", "2026-01-01", "2026-01-01"],
        "mw": [10, 12, 20, 30, 35],
    })

    result = duplicate_key_context(df, keys=["project_id", "snapshot_date"], subject="project_snapshot")

    assert result["passed"] is False
    assert result["metrics"]["key_columns"] == ["project_id", "snapshot_date"]
    assert result["metrics"]["unique_key_count"] == 3
    assert result["metrics"]["duplicate_key_count"] == 2
    assert result["metrics"]["duplicate_row_count"] == 4
    assert result["samples"]["duplicate_keys"] == [
        {
            "key": {"project_id": "A", "snapshot_date": "2026-01-01"},
            "row_count": 2,
            "excess_row_count": 1,
        },
        {
            "key": {"project_id": "B", "snapshot_date": "2026-01-01"},
            "row_count": 2,
            "excess_row_count": 1,
        },
    ]


def test_null_key_rows_are_reported_as_risk():
    df = pd.DataFrame({
        "customer_id": [1, None, 2, None],
        "amount": [10, 20, 30, 40],
    })

    result = duplicate_key_context(df, keys=["customer_id"], subject="customer_facts")

    assert result["passed"] is False
    assert result["has_null_keys"] is True
    assert result["metrics"]["null_key_row_count"] == 2
    assert result["metrics"]["all_null_key_row_count"] == 2
    assert result["metrics"]["null_counts_by_key"] == {"customer_id": 2}
    assert result["metrics"]["duplicate_key_count"] == 1
    assert result["samples"]["duplicate_keys"] == [
        {"key": {"customer_id": None}, "row_count": 2, "excess_row_count": 1}
    ]
    assert any("Null key values" in risk for risk in result["risks"])


def test_null_keys_can_be_excluded_from_duplicate_grouping_but_still_reported():
    df = pd.DataFrame({
        "customer_id": [1, None, 2, None],
        "amount": [10, 20, 30, 40],
    })

    result = duplicate_key_context(
        df,
        keys=["customer_id"],
        treat_nulls_as_duplicates=False,
    )

    assert result["passed"] is False
    assert result["has_duplicates"] is False
    assert result["has_null_keys"] is True
    assert result["metrics"]["unique_key_count"] == 2
    assert result["metrics"]["duplicate_key_count"] == 0
    assert result["metrics"]["null_key_row_count"] == 2
    assert result["samples"] == {}


def test_partial_null_composite_keys_are_reported():
    df = pd.DataFrame({
        "project_id": ["A", "A", "B", None],
        "snapshot_date": [None, None, "2026-01-01", "2026-01-01"],
    })

    result = duplicate_key_context(df, keys=["project_id", "snapshot_date"])

    assert result["has_null_keys"] is True
    assert result["metrics"]["null_key_row_count"] == 3
    assert result["metrics"]["all_null_key_row_count"] == 0
    assert result["metrics"]["null_counts_by_key"] == {"project_id": 1, "snapshot_date": 2}
    assert result["metrics"]["duplicate_key_count"] == 1
    assert result["samples"]["duplicate_keys"] == [
        {
            "key": {"project_id": "A", "snapshot_date": None},
            "row_count": 2,
            "excess_row_count": 1,
        }
    ]


def test_empty_dataframe_returns_valid_zero_context():
    df = pd.DataFrame({
        "order_id": pd.Series([], dtype="int64"),
        "amount": pd.Series([], dtype="float64"),
    })

    result = duplicate_key_context(df, keys=["order_id"], subject="empty_orders")

    assert result["passed"] is True
    assert result["metrics"]["row_count"] == 0
    assert result["metrics"]["unique_key_count"] == 0
    assert result["metrics"]["duplicate_key_count"] == 0
    assert result["metrics"]["duplicate_row_count"] == 0
    assert result["metrics"]["null_key_row_count"] == 0
    assert result["samples"] == {}
    assert result["risks"] == []
    assert "no rows" in result["summary"]


def test_sample_limit_is_respected():
    df = pd.DataFrame({
        "id": [1, 1, 2, 2, 3, 3, 4, 4],
        "value": list(range(8)),
    })

    result = duplicate_key_context(df, keys=["id"], sample_limit=2)

    assert result["metrics"]["duplicate_key_count"] == 4
    assert result["metrics"]["sampled_duplicate_key_count"] == 2
    assert len(result["samples"]["duplicate_keys"]) == 2


def test_zero_sample_limit_returns_no_key_samples():
    df = pd.DataFrame({"id": [1, 1, 2, 2]})

    result = duplicate_key_context(df, keys=["id"], sample_limit=0)

    assert result["metrics"]["duplicate_key_count"] == 2
    assert result["metrics"]["sampled_duplicate_key_count"] == 0
    assert result["samples"] == {}


def test_include_duplicate_rows_returns_capped_row_examples():
    df = pd.DataFrame({
        "id": [1, 1, 1, 2, 2, 3],
        "value": ["a", "b", "c", "d", "e", "f"],
    })

    result = duplicate_key_context(
        df,
        keys=["id"],
        include_duplicate_rows=True,
        row_sample_limit=3,
    )

    assert len(result["samples"]["duplicate_rows"]) == 3
    assert result["samples"]["duplicate_rows"][0]["id"] == 1
    assert result["samples"]["duplicate_rows"][0]["_duplicate_group_row_count"] == 3


def test_json_safe_output_for_timestamps_and_numpy_values():
    df = pd.DataFrame({
        "project_id": np.array([1, 1, 2], dtype=np.int64),
        "snapshot_date": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")],
        "amount": np.array([10.5, 11.5, 20.0], dtype=np.float64),
    })

    result = duplicate_key_context(
        df,
        keys=["project_id", "snapshot_date"],
        include_duplicate_rows=True,
    )

    json.dumps(result)
    assert result["samples"]["duplicate_keys"][0]["key"] == {"project_id": 1, "snapshot_date": "2026-01-01T00:00:00"}
    assert result["samples"]["duplicate_rows"][0]["snapshot_date"] == "2026-01-01T00:00:00"


def test_missing_key_column_raises_clear_error():
    df = pd.DataFrame({"id": [1, 2, 3]})

    with pytest.raises(ValueError, match="Missing key column"):
        duplicate_key_context(df, keys=["missing_id"])


@pytest.mark.parametrize("keys", [[], [""], ["id", "id"]])
def test_invalid_keys_raise_value_error(keys):
    df = pd.DataFrame({"id": [1, 2, 3]})

    with pytest.raises(ValueError):
        duplicate_key_context(df, keys=keys)


def test_non_string_key_raises_type_error():
    df = pd.DataFrame({"id": [1, 2, 3]})

    with pytest.raises(TypeError):
        duplicate_key_context(df, keys=["id", 123])


@pytest.mark.parametrize("kwargs", [{"sample_limit": -1}, {"row_sample_limit": -1}])
def test_negative_sample_limits_raise_value_error(kwargs):
    df = pd.DataFrame({"id": [1, 1]})

    with pytest.raises(ValueError):
        duplicate_key_context(df, keys=["id"], **kwargs)


@pytest.mark.parametrize("kwargs", [{"sample_limit": 1.5}, {"row_sample_limit": "5"}, {"sample_limit": True}])
def test_non_integer_sample_limits_raise_type_error(kwargs):
    df = pd.DataFrame({"id": [1, 1]})

    with pytest.raises(TypeError):
        duplicate_key_context(df, keys=["id"], **kwargs)


def test_engine_auto_detects_pandas():
    df = pd.DataFrame({"id": [1, 2, 3]})

    result = duplicate_key_context(df, keys=["id"], engine="auto")

    assert result["metrics"]["engine"] == "pandas"


def test_engine_pandas_rejects_non_pandas_object():
    with pytest.raises(TypeError, match="Expected pandas DataFrame"):
        duplicate_key_context([{"id": 1}], keys=["id"], engine="pandas")


def test_engine_spark_rejects_pandas_dataframe():
    """Spark engine raises TypeError when given a pandas DataFrame."""
    df = pd.DataFrame({"id": [1, 1]})

    with pytest.raises(TypeError, match="Expected Spark DataFrame"):
        duplicate_key_context(df, keys=["id"], engine="spark")


def test_unsupported_engine_raises_value_error():
    df = pd.DataFrame({"id": [1, 2, 3]})

    with pytest.raises(ValueError, match="Unsupported engine"):
        duplicate_key_context(df, keys=["id"], engine="duckdb")


@pytest.fixture(scope="module")
def spark_session():
    """A local SparkSession, or skip when no JVM/pyspark is available.

    Spark requires a Java runtime; on machines without one (most local dev),
    these tests skip. They run for real on Databricks / any JVM-backed env.
    """
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        session = (
            SparkSession.builder
            .master("local[1]")
            .appName("anchor-dupkey-tests")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "1")
            .getOrCreate()
        )
    except Exception as exc:  # no JVM, gateway failure, etc.
        pytest.skip(f"Local SparkSession unavailable: {exc}")
    yield session
    try:
        session.stop()
    except Exception:
        pass


def test_spark_matches_pandas_contract(spark_session):
    """Spark and pandas engines produce an identical contract for the same data.

    The metrics dicts are identical except for the 'engine' tag, so the strongest
    parity check is direct equality after dropping that key.
    """
    data = [
        {"id": 1, "v": "a"},
        {"id": 1, "v": "b"},
        {"id": 2, "v": "c"},
        {"id": 3, "v": "d"},
        {"id": None, "v": "e"},
    ]
    pdf = pd.DataFrame(data)
    sdf = spark_session.createDataFrame(data)

    pandas_ctx = duplicate_key_context(pdf, keys=["id"], engine="pandas", output_format="dict")
    spark_ctx = duplicate_key_context(sdf, keys=["id"], engine="spark", output_format="dict")

    # Identical top-level contract shape.
    assert set(spark_ctx.keys()) == set(pandas_ctx.keys())
    assert spark_ctx["kind"] == pandas_ctx["kind"]

    # Engine-independent verdict flags match.
    assert spark_ctx["has_duplicates"] == pandas_ctx["has_duplicates"] is True
    assert spark_ctx["has_null_keys"] == pandas_ctx["has_null_keys"] is True
    assert spark_ctx["passed"] == pandas_ctx["passed"] is False

    # Metrics are identical except the engine tag.
    p_metrics = {k: v for k, v in pandas_ctx["metrics"].items() if k != "engine"}
    s_metrics = {k: v for k, v in spark_ctx["metrics"].items() if k != "engine"}
    assert s_metrics == p_metrics
    assert spark_ctx["metrics"]["engine"] == "spark"
    assert pandas_ctx["metrics"]["engine"] == "pandas"

    # Output stays JSON-serializable.
    json.dumps(spark_ctx)
