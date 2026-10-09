"""Tests for odibi_anchor.tables.table_contract_summary."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from odibi_anchor.tables import table_contract_summary


@pytest.fixture
def store_sales_df() -> pd.DataFrame:
    """Representative business dataset with key, freshness, dimensions, and measures."""
    return pd.DataFrame(
        {
            "store_id": ["S1", "S2", "S3", "S4"],
            "store_name": ["Downtown", "Airport", "Harbor", "Uptown"],
            "region": ["NORTH", "SOUTH", "EAST", "WEST"],
            "order_total": [100.0, 250.5, 300.25, 410.0],
            "is_active": [True, True, False, True],
            "updated_at": pd.to_datetime(
                ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"],
                utc=True,
            ),
        }
    )


def test_table_contract_summary_returns_expected_contract(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(
        store_sales_df,
        subject="analytics.stores",
        candidate_key_columns=["store_id"],
        reference_time="2026-05-07T00:00:00Z",
    )

    assert ctx["kind"] == "table_contract_summary"
    assert ctx["subject"] == "analytics.stores"
    assert ctx["metrics"]["row_count"] == 4
    assert ctx["metrics"]["column_count"] == 6
    assert ctx["metrics"]["profiled_column_count"] == 6
    assert ctx["metrics"]["sample_column_count"] == 6
    assert ctx["candidate_keys"]["provided_key"]["status"] == "unique"
    assert ctx["candidate_keys"]["provided_key"]["is_unique"] is True
    assert ctx["freshness"]["selected_column"] == "updated_at"
    assert ctx["freshness"]["latest_value"] == "2026-05-04T00:00:00+00:00"
    assert ctx["freshness"]["latest_age_days"] == 3.0
    assert len(ctx["schema"]) == 6
    assert len(ctx["profile"]["columns"]) == 6
    assert len(ctx["samples"]["data_preview"]) == 4
    assert "analytics.stores has 4 rows and 6 columns" in ctx["summary"]


def test_output_is_json_serializable(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(
        store_sales_df,
        candidate_key_columns=["store_id"],
        reference_time="2026-05-07T00:00:00Z",
    )

    json.dumps(ctx)


def test_sample_limit_caps_samples(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df, sample_limit=2)

    assert len(ctx["samples"]["data_preview"]) == 2
    assert ctx["metrics"]["sample_row_count"] == 2


def test_sample_limit_zero_returns_no_samples(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df, sample_limit=0)

    assert ctx["samples"] == {}
    assert ctx["metrics"]["sample_row_count"] == 0


def test_infers_single_column_key_when_no_key_provided(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df, reference_time="2026-05-07T00:00:00Z")

    assert "store_id" in ctx["candidate_keys"]["inferred_single_column_keys"]
    assert "order_total" not in ctx["candidate_keys"]["inferred_single_column_keys"]
    assert any("Likely single-column key" in finding["message"] for finding in ctx["findings"])


def test_duplicate_candidate_key_is_flagged() -> None:
    df = pd.DataFrame(
        {
            "store_id": ["S1", "S1", "S2", "S3"],
            "load_date": pd.to_datetime(["2026-05-01"] * 4),
            "value": [10, 12, 20, 30],
        }
    )

    ctx = table_contract_summary(
        df,
        candidate_key_columns=["store_id"],
        reference_time="2026-05-07T00:00:00Z",
    )

    key = ctx["candidate_keys"]["provided_key"]
    assert key["status"] == "duplicate_keys_found"
    assert key["is_unique"] is False
    assert key["duplicate_key_row_count"] == 2
    assert key["duplicate_key_group_count"] == 1
    assert key["duplicate_key_samples"] == [{"store_id": "S1"}]
    assert any("duplicate-key rows" in risk["message"] for risk in ctx["risks"])


def test_null_candidate_key_is_flagged() -> None:
    df = pd.DataFrame(
        {
            "store_id": ["S1", None, "S3"],
            "event_timestamp": pd.to_datetime(["2026-05-01", "2026-05-02", "2026-05-03"]),
            "value": [10, 20, 30],
        }
    )

    ctx = table_contract_summary(
        df,
        candidate_key_columns=["store_id"],
        reference_time="2026-05-07T00:00:00Z",
    )

    key = ctx["candidate_keys"]["provided_key"]
    assert key["status"] == "null_keys_found"
    assert key["null_key_row_count"] == 1
    assert key["null_key_samples"] == [{"store_id": None}]
    assert any("null key" in risk["message"] for risk in ctx["risks"])


def test_multiple_null_keys_are_not_counted_as_duplicate_keys() -> None:
    df = pd.DataFrame(
        {
            "store_id": ["S1", None, None, "S2"],
            "updated_at": pd.to_datetime(["2026-05-01"] * 4),
            "value": [10, 20, 30, 40],
        }
    )

    ctx = table_contract_summary(df, candidate_key_columns=["store_id"])
    key = ctx["candidate_keys"]["provided_key"]

    assert key["status"] == "null_keys_found"
    assert key["null_key_row_count"] == 2
    assert key["duplicate_key_row_count"] == 0


def test_composite_candidate_key() -> None:
    df = pd.DataFrame(
        {
            "store_id": ["S1", "S1", "S2", "S2"],
            "measurement_date": ["2026-05-01", "2026-05-02", "2026-05-01", "2026-05-02"],
            "value": [10, 11, 20, 21],
        }
    )

    ctx = table_contract_summary(
        df,
        candidate_key_columns=["store_id", "measurement_date"],
        reference_time="2026-05-07T00:00:00Z",
    )

    key = ctx["candidate_keys"]["provided_key"]
    assert key["status"] == "unique"
    assert key["columns"] == ["store_id", "measurement_date"]
    assert key["duplicate_key_row_count"] == 0


def test_high_null_and_all_null_columns_are_flagged() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "optional_code": [None, None, "X", None],
            "empty_col": [None, None, None, None],
            "updated_at": pd.to_datetime(["2026-05-01"] * 4),
        }
    )

    ctx = table_contract_summary(
        df,
        candidate_key_columns=["id"],
        high_null_rate_threshold=0.50,
        reference_time="2026-05-07T00:00:00Z",
    )

    assert ctx["metrics"]["all_null_column_count"] == 1
    assert ctx["metrics"]["high_null_column_count"] == 2
    assert any("All-null columns" in risk["message"] for risk in ctx["risks"])
    assert any("high-null threshold" in risk["message"] for risk in ctx["risks"])


def test_high_null_metric_respects_configured_threshold() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "nullable_40_pct": ["A", "B", "C", None, None],
            "updated_at": pd.to_datetime(["2026-05-01"] * 5),
        }
    )

    ctx = table_contract_summary(df, high_null_rate_threshold=0.40)

    assert ctx["metrics"]["high_null_column_count"] == 1
    assert any("nullable_40_pct" in risk.get("columns", []) for risk in ctx["risks"])


def test_empty_dataframe_returns_contract_and_risks() -> None:
    df = pd.DataFrame(
        {
            "store_id": pd.Series([], dtype="object"),
            "updated_at": pd.Series([], dtype="datetime64[ns]"),
            "value": pd.Series([], dtype="float64"),
        }
    )

    ctx = table_contract_summary(
        df,
        candidate_key_columns=["store_id"],
        reference_time="2026-05-07T00:00:00Z",
    )

    assert ctx["metrics"]["row_count"] == 0
    assert ctx["samples"] == {}
    assert ctx["candidate_keys"]["provided_key"]["status"] == "empty"
    assert ctx["freshness"]["selected_column"] == "updated_at"
    assert ctx["freshness"]["latest_value"] is None
    assert any("zero rows" in risk["message"] for risk in ctx["risks"])


def test_wide_dataframe_profiles_only_max_columns() -> None:
    df = pd.DataFrame({f"col_{i}": [i, i + 1] for i in range(8)})

    ctx = table_contract_summary(df, max_profile_columns=3)

    assert ctx["metrics"]["column_count"] == 8
    assert ctx["metrics"]["profiled_column_count"] == 3
    assert ctx["metrics"]["omitted_column_count"] == 5
    assert ctx["profile"]["omitted_columns"] == ["col_3", "col_4", "col_5", "col_6", "col_7"]
    assert len(ctx["schema"]) == 3
    assert len(ctx["samples"]["data_preview"][0]) == 3
    assert any("omitted" in risk["message"] for risk in ctx["risks"])


def test_profile_columns_allow_explicit_subset(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(
        store_sales_df,
        profile_columns=["store_id", "order_total"],
        sample_limit=1,
    )

    assert ctx["profile"]["selection_mode"] == "explicit"
    assert ctx["profile"]["profiled_columns"] == ["store_id", "order_total"]
    assert ctx["metrics"]["unprofiled_table_column_count"] == 4
    assert set(ctx["samples"]["data_preview"][0]) == {"store_id", "order_total"}


def test_sample_columns_can_override_profiled_columns(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(
        store_sales_df,
        profile_columns=["store_id", "order_total"],
        sample_columns=["store_id", "updated_at"],
        sample_limit=1,
        reference_time="2026-05-07T00:00:00Z",
    )

    assert ctx["metrics"]["sample_column_count"] == 2
    assert set(ctx["samples"]["data_preview"][0]) == {"store_id", "updated_at"}


def test_string_freshness_column_is_detected_by_name() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "load_date": ["2026-05-01", "2026-05-05", "bad-date"],
            "value": [10, 20, 30],
        }
    )

    ctx = table_contract_summary(df, reference_time="2026-05-07T00:00:00Z")

    assert ctx["freshness"]["selected_column"] == "load_date"
    assert ctx["freshness"]["latest_value"] == "2026-05-05T00:00:00+00:00"
    assert ctx["freshness"]["columns_checked"][0]["parse_success_rate"] == 0.6667


def test_explicit_freshness_columns_can_override_name_detection() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "business_period": ["2026-01-01", "2026-02-01", "2026-03-01"],
            "not_a_date": ["x", "y", "z"],
        }
    )

    ctx = table_contract_summary(
        df,
        freshness_columns=["business_period"],
        reference_time="2026-05-07T00:00:00Z",
    )

    assert ctx["freshness"]["selected_column"] == "business_period"
    assert ctx["freshness"]["latest_value"] == "2026-03-01T00:00:00+00:00"


def test_unusable_freshness_column_is_reported_not_selected() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "load_date": ["bad", "also bad", "2026-05-01"],
            "value": [10, 20, 30],
        }
    )

    ctx = table_contract_summary(df, freshness_columns=["load_date"])

    assert ctx["freshness"]["selected_column"] is None
    assert ctx["freshness"]["columns_checked"][0]["is_usable"] is False
    assert any("could not be parsed reliably" in risk["message"] for risk in ctx["risks"])


def test_stale_freshness_is_flagged_when_threshold_is_provided() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2],
            "updated_at": pd.to_datetime(["2026-01-01", "2026-01-15"]),
        }
    )

    ctx = table_contract_summary(
        df,
        reference_time="2026-05-07T00:00:00Z",
        stale_after_days=30,
    )

    assert ctx["freshness"]["is_stale"] is True
    assert ctx["freshness"]["latest_age_days"] == 112.0
    assert any("exceeds stale_after_days" in risk["message"] for risk in ctx["risks"])


def test_future_freshness_is_flagged() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2],
            "updated_at": pd.to_datetime(["2026-05-08", "2026-05-09"], utc=True),
        }
    )

    ctx = table_contract_summary(df, reference_time="2026-05-07T00:00:00Z")

    assert ctx["freshness"]["is_future_dated"] is True
    assert ctx["freshness"]["latest_age_days"] == -2.0
    assert any("future" in risk["message"] for risk in ctx["risks"])


def test_no_freshness_column_is_a_medium_risk() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})

    ctx = table_contract_summary(df)

    assert ctx["freshness"]["selected_column"] is None
    assert any(risk["message"] == "No freshness column was detected." for risk in ctx["risks"])


def test_value_examples_can_be_disabled(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df, include_value_examples=False)

    assert "top_values" not in ctx["profile"]["columns"][0]


def test_numeric_stats_are_included_for_numeric_columns(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df)
    total_profile = next(column for column in ctx["profile"]["columns"] if column["name"] == "order_total")

    assert total_profile["likely_role"] == "measure"
    assert total_profile["numeric_stats"]["min"] == 100.0
    assert total_profile["numeric_stats"]["max"] == 410.0
    assert total_profile["numeric_stats"]["mean"] == 265.1875


def test_boolean_columns_are_flags_not_numeric_measures(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df)
    active_profile = next(column for column in ctx["profile"]["columns"] if column["name"] == "is_active")

    assert active_profile["likely_role"] == "flag"
    assert "numeric_stats" not in active_profile


def test_long_string_samples_are_truncated() -> None:
    long_text = "x" * 120
    df = pd.DataFrame({"id": [1], "updated_at": pd.to_datetime(["2026-05-01"]), "notes": [long_text]})

    ctx = table_contract_summary(df, sample_limit=1)

    assert ctx["samples"]["data_preview"][0]["notes"].endswith("...")
    assert len(ctx["samples"]["data_preview"][0]["notes"]) == 80


def test_unhashable_column_values_do_not_crash() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "updated_at": pd.to_datetime(["2026-05-01", "2026-05-02", "2026-05-03"]),
            "payload": [{"a": 1}, {"a": 1}, {"b": 2}],
        }
    )

    ctx = table_contract_summary(df, reference_time="2026-05-07T00:00:00Z")
    payload_profile = next(column for column in ctx["profile"]["columns"] if column["name"] == "payload")

    assert payload_profile["distinct_count"] == 2
    assert payload_profile["top_values"][0]["count"] == 2
    assert ctx["metrics"]["duplicate_row_count"] is None
    json.dumps(ctx)


def test_constant_columns_are_reported() -> None:
    df = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "source_system": ["erp", "erp", "erp"],
            "updated_at": pd.to_datetime(["2026-05-01"] * 3),
        }
    )

    ctx = table_contract_summary(df)

    assert ctx["metrics"]["constant_column_count"] == 2
    assert any("Constant columns" in finding["message"] for finding in ctx["findings"])


def test_missing_candidate_key_column_raises_value_error(store_sales_df: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="candidate_key_columns not found"):
        table_contract_summary(store_sales_df, candidate_key_columns=["missing_id"])


def test_missing_freshness_column_raises_value_error(store_sales_df: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="freshness_columns not found"):
        table_contract_summary(store_sales_df, freshness_columns=["missing_ts"])


def test_missing_profile_column_raises_value_error(store_sales_df: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="profile_columns not found"):
        table_contract_summary(store_sales_df, profile_columns=["missing_col"])


def test_missing_sample_column_raises_value_error(store_sales_df: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="sample_columns not found"):
        table_contract_summary(store_sales_df, sample_columns=["missing_col"])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sample_limit": -1}, "sample_limit"),
        ({"max_profile_columns": 0}, "max_profile_columns"),
        ({"high_null_rate_threshold": 1.5}, "high_null_rate_threshold"),
        ({"stale_after_days": -1}, "stale_after_days"),
    ],
)
def test_invalid_limits_raise_value_error(store_sales_df: pd.DataFrame, kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        table_contract_summary(store_sales_df, **kwargs)


@pytest.mark.parametrize(
    "column_arg",
    ["candidate_key_columns", "freshness_columns", "profile_columns", "sample_columns"],
)
def test_duplicate_column_configuration_raises_value_error(
    store_sales_df: pd.DataFrame,
    column_arg: str,
) -> None:
    with pytest.raises(ValueError, match="duplicate column names"):
        table_contract_summary(store_sales_df, **{column_arg: ["store_id", "store_id"]})


@pytest.mark.parametrize(
    "column_arg",
    ["candidate_key_columns", "freshness_columns", "profile_columns", "sample_columns"],
)
def test_non_string_column_configuration_raises_type_error(
    store_sales_df: pd.DataFrame,
    column_arg: str,
) -> None:
    with pytest.raises(TypeError, match="must contain only strings"):
        table_contract_summary(store_sales_df, **{column_arg: ["store_id", 123]})  # type: ignore[list-item]


def test_duplicate_dataframe_columns_raise_value_error() -> None:
    df = pd.DataFrame([[1, 2]], columns=["id", "id"])

    with pytest.raises(ValueError, match="DataFrame contains duplicate column names"):
        table_contract_summary(df)


def test_non_string_dataframe_columns_raise_type_error() -> None:
    df = pd.DataFrame([[1, 2]], columns=["id", 123])

    with pytest.raises(TypeError, match="DataFrame columns must all be strings"):
        table_contract_summary(df)


def test_explicit_pandas_engine_works(store_sales_df: pd.DataFrame) -> None:
    ctx = table_contract_summary(store_sales_df, engine="pandas")

    assert ctx["kind"] == "table_contract_summary"


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
            .appName("anchor-contract-tests")
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


def test_spark_engine_matches_pandas_contract(spark_session) -> None:
    """The Spark path produces the same contract shape and core metrics as pandas.

    Uses a small dataset (well under spark_sample_size) so the hybrid path's
    sampled profiling is computed over the full data — shared values must match
    the pandas path, with only the documented spark-specific metric additions
    (source_engine, spark_sample_size) and the dropped memory_bytes.
    """
    data = [
        {"store_id": "S1", "region": "NORTH", "order_total": 100.0, "event_date": "2026-05-01"},
        {"store_id": "S2", "region": "SOUTH", "order_total": 250.5, "event_date": "2026-05-02"},
        {"store_id": "S3", "region": "EAST", "order_total": 300.25, "event_date": "2026-05-03"},
        {"store_id": "S1", "region": "WEST", "order_total": 410.0, "event_date": "2026-05-04"},
    ]
    columns = ["store_id", "region", "order_total", "event_date"]
    pdf = pd.DataFrame(data, columns=columns)
    sdf = spark_session.createDataFrame(data).select(*columns)

    pandas_ctx = table_contract_summary(
        pdf, subject="t", candidate_key_columns=["store_id"], engine="pandas"
    )
    spark_ctx = table_contract_summary(
        sdf, subject="t", candidate_key_columns=["store_id"], engine="spark"
    )

    # Identical top-level contract shape.
    assert set(spark_ctx.keys()) == set(pandas_ctx.keys())
    assert spark_ctx["kind"] == pandas_ctx["kind"] == "table_contract_summary"

    # Exact shared metrics from the full table.
    assert spark_ctx["metrics"]["row_count"] == pandas_ctx["metrics"]["row_count"] == 4
    assert spark_ctx["metrics"]["column_count"] == pandas_ctx["metrics"]["column_count"]
    # store_id has a duplicate (S1 x2) → not unique on either engine.
    assert spark_ctx["metrics"]["provided_key_is_unique"] is False
    assert pandas_ctx["metrics"]["provided_key_is_unique"] is False

    # Spark-specific metric additions are present.
    assert spark_ctx["metrics"]["source_engine"] == "spark"
    assert spark_ctx["metrics"]["spark_sample_size"] == 4

    # Same profiled columns, and output stays JSON-serializable.
    assert (
        [c["name"] for c in spark_ctx["profile"]["columns"]]
        == [c["name"] for c in pandas_ctx["profile"]["columns"]]
    )
    json.dumps(spark_ctx)


def test_unknown_engine_raises_value_error(store_sales_df: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="engine must be one of"):
        table_contract_summary(store_sales_df, engine="duckdb")


def test_non_dataframe_input_raises_type_error() -> None:
    with pytest.raises(TypeError, match="Expected pandas or Spark DataFrame"):
        table_contract_summary([{"id": 1}])


def test_numpy_scalar_and_nan_values_are_serialized() -> None:
    df = pd.DataFrame(
        {
            "id": np.array([1, 2, 3], dtype=np.int64),
            "updated_at": pd.to_datetime(["2026-05-01", "2026-05-02", "2026-05-03"]),
            "value": [np.float64(1.25), np.nan, np.float64(3.75)],
        }
    )

    ctx = table_contract_summary(df, sample_limit=3, reference_time="2026-05-07T00:00:00Z")

    assert ctx["samples"]["data_preview"][1]["value"] is None
    json.dumps(ctx)
