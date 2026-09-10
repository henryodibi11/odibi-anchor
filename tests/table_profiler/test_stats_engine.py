"""Tests for the foundational column statistics engine."""

from __future__ import annotations

from tools.table_profiler_tool.lib.stats_engine import column_profiles_as_dicts
from tools.table_profiler_tool.lib.stats_engine import compute_column_stats


def test_compute_column_stats_returns_profiles_for_clean_fact_df(clean_fact_df):
    """Verify clean fact-like data produces expected foundational profile stats."""

    profiles = compute_column_stats(clean_fact_df)

    assert len(profiles) == 5

    by_name = {profile.name: profile for profile in profiles}

    invoice_profile = by_name["invoice_id"]
    assert invoice_profile.row_count == 4
    assert invoice_profile.null_count == 0
    assert invoice_profile.distinct_count == 4
    assert invoice_profile.is_unique is True
    assert invoice_profile.min_length == 7
    assert invoice_profile.max_length == 7
    assert len(invoice_profile.sample_values) <= 5

    amount_profile = by_name["amount"]
    assert amount_profile.mean_value == 131.4375
    assert amount_profile.min_value == 75.0
    assert amount_profile.max_value == 200.5
    assert amount_profile.median_value is not None
    assert round(amount_profile.median_value, 3) == 125.125
    assert amount_profile.std_value is not None
    assert amount_profile.min_length is None

    status_profile = by_name["status"]
    assert status_profile.distinct_count == 3
    assert status_profile.is_constant is False
    assert status_profile.top_values[0]["value"] == "Open"
    assert status_profile.top_values[0]["count"] == 2
    assert round(status_profile.top_values[0]["pct"], 2) == 0.5



def test_compute_column_stats_handles_dirty_strings_and_nulls(dirty_excel_like_df):
    """Verify dirty string columns surface null and length signals."""

    profiles = compute_column_stats(dirty_excel_like_df)
    by_name = {profile.name: profile for profile in profiles}

    project_code = by_name["project_code"]
    assert project_code.row_count == 5
    assert project_code.non_null_count == 4
    assert project_code.null_count == 1
    assert round(project_code.null_pct, 2) == 0.2
    assert project_code.distinct_count == 4
    assert project_code.is_unique is True
    assert project_code.min_length == 7
    assert project_code.max_length == 8

    status = by_name["status"]
    assert status.distinct_count == 5
    assert status.is_unique is True
    assert status.min_length == 3
    assert status.max_length == 9
    assert any(value in status.sample_values for value in ["Active", "Withdrawn", "TBD"])

    entered_on = by_name["entered_on"]
    assert entered_on.null_count == 1
    assert entered_on.min_length == 7
    assert entered_on.max_length == 10
    assert entered_on.top_values[0]["count"] == 1



def test_column_profiles_as_dicts_returns_json_friendly_shapes(clean_fact_df):
    """Verify dict export returns serializable profile-shaped payloads."""

    profile_dicts = column_profiles_as_dicts(clean_fact_df)

    assert len(profile_dicts) == 5
    assert all(isinstance(item, dict) for item in profile_dicts)

    amount_dict = next(item for item in profile_dicts if item["name"] == "amount")
    assert amount_dict["spark_type"] == "float64"
    assert amount_dict["semantic_type"] == "unknown"
    assert amount_dict["mean_value"] == 131.4375
    assert isinstance(amount_dict["top_values"], list)
    assert isinstance(amount_dict["sample_values"], list)


def test_compute_column_stats_alphanumeric_string_no_numeric_stats():
    """Regression: alphanumeric strings like '23INR0287' must not crash stats.

    In the Spark path, F.mean/F.stddev_samp/F.percentile_approx attempted an
    implicit CAST of string columns to DOUBLE, raising CAST_INVALID_INPUT for
    values like '23INR0287'.  The pandas path uses pd.to_numeric(errors='coerce')
    which is already safe — this test documents the expected contract:
    string-typed columns must have mean/std/median=None regardless of content.
    """
    import pandas as pd

    df = pd.DataFrame({
        "inr": ["23INR0287", "24INR0001", "25INR9999", "23INR0100"],
        "mw": [10.0, 20.0, 30.0, 40.0],
    })
    profiles = compute_column_stats(df)
    by_name = {p.name: p for p in profiles}

    inr = by_name["inr"]
    assert inr.mean_value is None, "string col must not have mean_value"
    assert inr.std_value is None, "string col must not have std_value"
    assert inr.median_value is None, "string col must not have median_value"
    assert inr.min_length is not None, "string col must have min_length"
    assert inr.max_length is not None, "string col must have max_length"

    mw = by_name["mw"]
    assert mw.mean_value == 25.0, "numeric col must have mean_value"
    assert mw.min_length is None, "numeric col must not have min_length"
