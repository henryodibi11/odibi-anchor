"""Microscope — column-level deep-dive investigation tool.

Goes deeper than ColumnProfile: full distribution, pattern analysis,
anomaly detection, value clustering, and actionable recommendations.

Public API
----------
microscope(df, column, *, subject=None, profile=None, sample_limit=20, bin_count=20) -> dict
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from ._common import NULL_LIKE_SENTINELS, fingerprint_value, to_json_safe, serialize_dict_safe, serialize_samples_safe
from .models import ColumnProfile, TableProfile

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SAMPLE_FOR_DISTRIBUTION = 100_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def microscope(
    df: Any,
    column: str,
    *,
    subject: str | None = None,
    profile: TableProfile | None = None,
    sample_limit: int = 20,
    bin_count: int = 20,
) -> dict:
    """Deep-dive investigation of a single column.

    Args:
        df: Spark or pandas DataFrame.
        column: Column name to investigate.
        subject: Optional label (defaults to column name).
        profile: Optional pre-computed TableProfile to avoid redundant stats.
            If provided, reuses the ColumnProfile for that column.
        sample_limit: Max example values per category in samples.
        bin_count: Number of histogram bins for numeric columns.

    Returns:
        Anchor standard contract dict with kind="microscope".
    """
    subject = subject or column

    # Validate column exists
    engine = detect_engine(df)
    cols = _get_columns(df, engine)
    if column not in cols:
        raise ValueError(
            f"Column '{column}' not found in DataFrame. "
            f"Available columns: {cols[:20]}"
        )

    # Convert to pandas for analysis
    pdf, sampling_info = _to_pandas_with_info(df, engine, column)
    series = pdf[column]

    # Get or build column profile
    col_profile = _get_col_profile(profile, column)

    # Determine column type category
    col_type = _classify_column_type(series, pdf, column, engine, df)

    # Run analyses
    metrics: dict[str, Any] = {}
    findings: list[str] = []
    risks: list[str] = []
    samples: dict[str, Any] = {}

    # Common analysis for all types
    _analyze_common(series, col_profile, metrics, findings, risks, samples, sample_limit)

    # Type-specific analysis
    if col_type == "numeric":
        _analyze_numeric(series, metrics, findings, risks, samples, bin_count, sample_limit)
    elif col_type == "string":
        _analyze_string(series, metrics, findings, risks, samples, sample_limit)
    elif col_type == "timestamp":
        _analyze_timestamp(series, metrics, findings, risks, samples, sample_limit)
    elif col_type == "boolean":
        _analyze_boolean(series, metrics, findings, risks, samples)

    # Add type info to metrics
    spark_type = _get_spark_type(df, column, engine)
    metrics["spark_type"] = spark_type
    metrics["column_type_category"] = col_type

    if col_profile:
        if col_profile.semantic_type and col_profile.semantic_type.value != "unknown":
            metrics["semantic_type"] = col_profile.semantic_type.value
        if col_profile.role and col_profile.role.value != "unknown":
            metrics["role"] = col_profile.role.value

    # Sampling metadata
    metrics["sampled"] = sampling_info["sampled"]
    if sampling_info["sampled"]:
        metrics["source_row_count"] = sampling_info["source_row_count"]
        metrics["sample_size"] = sampling_info["sample_size"]

    # If profile provided and we sampled, override with exact values from profile
    if col_profile and sampling_info["sampled"]:
        metrics["row_count"] = col_profile.row_count or metrics["row_count"]
        metrics["null_count"] = col_profile.null_count or metrics["null_count"]
        metrics["null_pct"] = round(col_profile.null_pct, 4) if col_profile.null_pct else metrics["null_pct"]
        metrics["distinct_count"] = col_profile.distinct_count or metrics["distinct_count"]
        metrics["distinct_pct"] = round(col_profile.distinct_pct, 4) if col_profile.distinct_pct else metrics["distinct_pct"]
        metrics["is_unique"] = col_profile.is_unique

    # Build summary line
    summary = _build_summary(column, col_type, spark_type, metrics)

    # Build suggested next actions
    next_actions = _build_next_actions(column, col_type, metrics, findings, risks)

    # Serialize and return
    result = {
        "kind": "microscope",
        "subject": subject,
        "summary": summary,
        "metrics": serialize_dict_safe(metrics),
        "findings": findings,
        "risks": risks,
        "samples": serialize_samples_safe(samples),
        "suggested_next_actions": next_actions,
    }
    return result


# ---------------------------------------------------------------------------
# Private: DataFrame helpers
# ---------------------------------------------------------------------------


def _get_columns(df: Any, engine: str) -> list[str]:
    """Get column names from DataFrame."""
    if engine == "spark":
        return df.columns
    return list(df.columns)


def _to_pandas_with_info(df: Any, engine: str, column: str) -> tuple[pd.DataFrame, dict]:
    """Convert to pandas, sampling if needed. Returns (df, sampling_info)."""
    if engine == "spark":
        row_count = df.count()
        if row_count > _MAX_SAMPLE_FOR_DISTRIBUTION:
            fraction = _MAX_SAMPLE_FOR_DISTRIBUTION / row_count
            sampled_df = df.select(column).sample(False, min(fraction * 1.1, 1.0), seed=42).limit(
                _MAX_SAMPLE_FOR_DISTRIBUTION
            ).toPandas()
            return sampled_df, {
                "sampled": True,
                "source_row_count": row_count,
                "sample_size": len(sampled_df),
            }
        return df.select(column).toPandas(), {
            "sampled": False,
            "source_row_count": row_count,
            "sample_size": row_count,
        }
    # pandas
    total_rows = len(df)
    if total_rows > _MAX_SAMPLE_FOR_DISTRIBUTION:
        sampled_df = df[[column]].sample(n=_MAX_SAMPLE_FOR_DISTRIBUTION, random_state=42)
        return sampled_df, {
            "sampled": True,
            "source_row_count": total_rows,
            "sample_size": _MAX_SAMPLE_FOR_DISTRIBUTION,
        }
    return df[[column]].copy(), {
        "sampled": False,
        "source_row_count": total_rows,
        "sample_size": total_rows,
    }


def _get_spark_type(df: Any, column: str, engine: str) -> str:
    """Get the Spark/pandas type string."""
    if engine == "spark":
        for field in df.schema.fields:
            if field.name == column:
                return str(field.dataType).replace("Type", "").lower()
    # pandas
    dtype = df[column].dtype
    return str(dtype)


def _get_col_profile(profile: TableProfile | None, column: str) -> ColumnProfile | None:
    """Extract ColumnProfile from TableProfile if available."""
    if profile and profile.columns:
        for cp in profile.columns:
            if cp.name == column:
                return cp
    return None


def _classify_column_type(series: pd.Series, pdf: pd.DataFrame, column: str, engine: str, df: Any) -> str:
    """Classify column into: numeric, string, timestamp, boolean."""
    dtype = series.dtype

    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_numeric_dtype(dtype):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "timestamp"

    # Check if object dtype might be boolean-like
    non_null = series.dropna()
    if len(non_null) > 0:
        unique_vals = set(non_null.astype(str).str.lower().unique())
        if unique_vals <= {"true", "false", "1", "0", "yes", "no", "y", "n"}:
            return "boolean"

    return "string"


# ---------------------------------------------------------------------------
# Private: Common analysis (all types)
# ---------------------------------------------------------------------------


def _analyze_common(
    series: pd.Series,
    col_profile: ColumnProfile | None,
    metrics: dict,
    findings: list,
    risks: list,
    samples: dict,
    sample_limit: int,
) -> None:
    """Analysis common to all column types."""
    row_count = len(series)
    null_count = int(series.isna().sum())
    null_pct = null_count / row_count if row_count > 0 else 0.0
    non_null = series.dropna()
    distinct_count = int(non_null.nunique())
    distinct_pct = distinct_count / len(non_null) if len(non_null) > 0 else 0.0
    is_unique = distinct_count == len(non_null) and len(non_null) > 0
    is_constant = distinct_count <= 1

    metrics["row_count"] = row_count
    metrics["null_count"] = null_count
    metrics["null_pct"] = round(null_pct, 4)
    metrics["distinct_count"] = distinct_count
    metrics["distinct_pct"] = round(distinct_pct, 4)
    metrics["is_unique"] = is_unique
    metrics["is_constant"] = is_constant

    # Findings about nulls
    if null_pct > 0:
        severity = "high" if null_pct > 0.2 else "moderate" if null_pct > 0.05 else "low"
        findings.append(f"{null_pct:.1%} null — {severity} null rate")

    if is_constant:
        findings.append(f"Column is constant — only value: {non_null.iloc[0] if len(non_null) > 0 else 'NULL'}")
    if is_unique:
        findings.append("Column is unique — potential key/ID column")

    # Top N values
    try:
        if len(non_null) > 0 and not is_unique:
            vc = non_null.value_counts()
            top_values = []
            for val, count in vc.head(sample_limit).items():
                top_values.append({
                    "value": to_json_safe(val),
                    "count": int(count),
                    "pct": round(count / row_count, 6),
                })
            samples["top_values"] = top_values

            # Bottom values (rarest)
            bottom_values = []
            for val, count in vc.tail(min(sample_limit, 10)).items():
                bottom_values.append({
                    "value": to_json_safe(val),
                    "count": int(count),
                    "pct": round(count / row_count, 6),
                })
            samples["bottom_values"] = bottom_values

            # Value concentration
            top5_pct = vc.head(5).sum() / row_count if row_count > 0 else 0
            top10_pct = vc.head(10).sum() / row_count if row_count > 0 else 0
            metrics["top5_concentration"] = round(top5_pct, 4)
            metrics["top10_concentration"] = round(top10_pct, 4)

            if top10_pct > 0.5:
                findings.append(f"Top 10 values cover {top10_pct:.0%} of rows — high concentration")
            elif top10_pct > 0.1:
                findings.append(f"Top 10 values cover {top10_pct:.0%} of rows — moderate concentration")
    except Exception:
        pass

    # Null-like detection (for string columns)
    try:
        if series.dtype == object:
            str_vals = non_null.astype(str).str.strip().str.lower()
            null_like_mask = str_vals.isin(NULL_LIKE_SENTINELS) | (non_null.astype(str).str.strip() == "")
            null_like_count = int(null_like_mask.sum())
            if null_like_count > 0:
                metrics["null_like_count"] = null_like_count
                # Find which null-like values exist
                null_like_vals = non_null[null_like_mask].value_counts()
                nl_samples = []
                for val, count in null_like_vals.head(10).items():
                    nl_samples.append({"value": str(val), "count": int(count)})
                samples["null_like_values"] = nl_samples
                findings.append(
                    f"{null_like_count} null-like values detected: "
                    f"{[s['value'] for s in nl_samples[:5]]} ({null_like_count:,} rows)"
                )
                risks.append(
                    "Null-like strings may cause silent join failures if not cleaned"
                )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Private: Numeric analysis
# ---------------------------------------------------------------------------


def _analyze_numeric(
    series: pd.Series,
    metrics: dict,
    findings: list,
    risks: list,
    samples: dict,
    bin_count: int,
    sample_limit: int,
) -> None:
    """Deep analysis for numeric columns."""
    non_null = series.dropna()
    if len(non_null) == 0:
        return

    # Descriptive stats
    try:
        metrics["min"] = float(non_null.min())
        metrics["max"] = float(non_null.max())
        metrics["mean"] = round(float(non_null.mean()), 6)
        metrics["median"] = float(non_null.median())
        metrics["std"] = round(float(non_null.std()), 6) if len(non_null) > 1 else 0.0
        metrics["skewness"] = round(float(non_null.skew()), 4) if len(non_null) > 2 else 0.0
        metrics["kurtosis"] = round(float(non_null.kurtosis()), 4) if len(non_null) > 3 else 0.0

        # Percentiles
        percentiles = [0.01, 0.05, 0.25, 0.75, 0.95, 0.99]
        pct_values = non_null.quantile(percentiles)
        for p, v in zip(percentiles, pct_values):
            key = f"p{int(p * 100)}"
            metrics[key] = round(float(v), 6)
    except Exception:
        pass

    # Histogram
    try:
        counts, bin_edges = np.histogram(non_null, bins=bin_count)
        histogram = []
        for i in range(len(counts)):
            histogram.append({
                "bin_start": round(float(bin_edges[i]), 6),
                "bin_end": round(float(bin_edges[i + 1]), 6),
                "count": int(counts[i]),
            })
        samples["histogram"] = histogram
        metrics["histogram_bin_count"] = bin_count
    except Exception:
        pass

    # Outlier detection (IQR method)
    try:
        q1 = float(non_null.quantile(0.25))
        q3 = float(non_null.quantile(0.75))
        iqr = q3 - q1
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        outlier_mask = (non_null < lower_bound) | (non_null > upper_bound)
        outlier_count = int(outlier_mask.sum())
        metrics["outlier_count"] = outlier_count
        metrics["outlier_pct"] = round(outlier_count / len(non_null), 4) if len(non_null) > 0 else 0.0
        metrics["iqr"] = round(iqr, 6)
        metrics["outlier_lower_bound"] = round(lower_bound, 6)
        metrics["outlier_upper_bound"] = round(upper_bound, 6)

        if outlier_count > 0:
            outlier_vals = non_null[outlier_mask].value_counts().head(sample_limit)
            outlier_samples = []
            for val, count in outlier_vals.items():
                outlier_samples.append({"value": float(val), "count": int(count)})
            samples["outliers"] = outlier_samples
            findings.append(
                f"{outlier_count} outliers detected ({metrics['outlier_pct']:.1%}) "
                f"outside IQR bounds [{lower_bound:.2f}, {upper_bound:.2f}]"
            )
    except Exception:
        pass

    # Zero analysis
    try:
        zero_count = int((non_null == 0).sum())
        zero_pct = zero_count / len(non_null) if len(non_null) > 0 else 0.0
        metrics["zero_count"] = zero_count
        metrics["zero_pct"] = round(zero_pct, 4)
        if zero_pct > 0.1:
            findings.append(f"{zero_pct:.0%} of values are zero")
    except Exception:
        pass

    # Negative value analysis
    try:
        neg_count = int((non_null < 0).sum())
        neg_pct = neg_count / len(non_null) if len(non_null) > 0 else 0.0
        metrics["negative_count"] = neg_count
        metrics["negative_pct"] = round(neg_pct, 4)
        if neg_count > 0:
            findings.append(f"{neg_pct:.0%} of values are negative ({neg_count:,} rows)")
    except Exception:
        pass

    # Precision analysis
    try:
        if non_null.dtype in (np.float64, np.float32, float):
            str_vals = non_null.astype(str)
            decimal_places = str_vals.apply(
                lambda x: len(x.split(".")[1]) if "." in x else 0
            )
            metrics["max_decimal_places"] = int(decimal_places.max())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Private: String analysis
# ---------------------------------------------------------------------------


def _analyze_string(
    series: pd.Series,
    metrics: dict,
    findings: list,
    risks: list,
    samples: dict,
    sample_limit: int,
) -> None:
    """Deep analysis for string columns."""
    non_null = series.dropna().astype(str)
    if len(non_null) == 0:
        return

    # Length distribution
    try:
        lengths = non_null.str.len()
        metrics["length_min"] = int(lengths.min())
        metrics["length_max"] = int(lengths.max())
        metrics["length_mean"] = round(float(lengths.mean()), 2)
        metrics["length_median"] = float(lengths.median())
        metrics["length_std"] = round(float(lengths.std()), 2) if len(lengths) > 1 else 0.0
        samples["length_distribution"] = {
            "min": metrics["length_min"],
            "max": metrics["length_max"],
            "mean": metrics["length_mean"],
            "median": metrics["length_median"],
            "std": metrics["length_std"],
        }
    except Exception:
        pass

    # Character class analysis
    try:
        total = len(non_null)
        alpha_only = int(non_null.str.match(r"^[a-zA-Z]+$").sum())
        alphanum = int(non_null.str.match(r"^[a-zA-Z0-9]+$").sum())
        has_digits = int(non_null.str.contains(r"\d", regex=True).sum())
        has_special = int(non_null.str.contains(r"[^a-zA-Z0-9\s]", regex=True).sum())
        metrics["char_alpha_only_pct"] = round(alpha_only / total, 4)
        metrics["char_alphanumeric_pct"] = round(alphanum / total, 4)
        metrics["char_has_digits_pct"] = round(has_digits / total, 4)
        metrics["char_has_special_pct"] = round(has_special / total, 4)
    except Exception:
        pass

    # Case analysis
    try:
        total = len(non_null)
        # Only analyze values that have letters
        has_letters = non_null[non_null.str.contains(r"[a-zA-Z]", regex=True)]
        if len(has_letters) > 0:
            all_upper = int(has_letters.str.match(r"^[^a-z]*$").sum())
            all_lower = int(has_letters.str.match(r"^[^A-Z]*$").sum())
            title_case = int(has_letters.str.match(r"^[A-Z][a-z]+(\s[A-Z][a-z]+)*$").sum())
            mixed = len(has_letters) - all_upper - all_lower - title_case
            metrics["case_upper_pct"] = round(all_upper / len(has_letters), 4)
            metrics["case_lower_pct"] = round(all_lower / len(has_letters), 4)
            metrics["case_title_pct"] = round(title_case / len(has_letters), 4)
            metrics["case_mixed_pct"] = round(max(0, mixed) / len(has_letters), 4)
    except Exception:
        pass

    # Pattern fingerprinting
    try:
        sample_for_patterns = non_null.head(min(10000, len(non_null)))
        patterns = sample_for_patterns.apply(fingerprint_value)
        pattern_counts = patterns.value_counts().head(sample_limit)
        pattern_samples = []
        for pat, count in pattern_counts.items():
            pattern_samples.append({
                "pattern": pat,
                "count": int(count),
                "pct": round(count / len(sample_for_patterns), 4),
            })
        samples["pattern_fingerprints"] = pattern_samples
    except Exception:
        pass

    # Whitespace issues
    try:
        leading_ws = int(non_null.str.match(r"^\s").sum())
        trailing_ws = int(non_null.str.match(r".*\s$").sum())
        double_spaces = int(non_null.str.contains(r"  ", regex=False).sum())
        ws_total = leading_ws + trailing_ws + double_spaces
        if ws_total > 0:
            metrics["whitespace_issues"] = ws_total
            metrics["leading_whitespace_count"] = leading_ws
            metrics["trailing_whitespace_count"] = trailing_ws
            findings.append(f"{ws_total} whitespace issues detected (leading={leading_ws}, trailing={trailing_ws})")
            if trailing_ws > 0 or leading_ws > 0:
                risks.append("Leading/trailing whitespace may break joins and lookups")
    except Exception:
        pass

    # Empty string count
    try:
        empty_count = int((non_null == "").sum())
        if empty_count > 0:
            metrics["empty_string_count"] = empty_count
            findings.append(f"{empty_count} empty strings detected (distinct from null)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Private: Timestamp analysis
# ---------------------------------------------------------------------------


def _analyze_timestamp(
    series: pd.Series,
    metrics: dict,
    findings: list,
    risks: list,
    samples: dict,
    sample_limit: int,
) -> None:
    """Deep analysis for timestamp/date columns."""
    non_null = series.dropna()
    if len(non_null) == 0:
        return

    # Ensure datetime
    try:
        if not pd.api.types.is_datetime64_any_dtype(non_null):
            non_null = pd.to_datetime(non_null, errors="coerce").dropna()
            if len(non_null) == 0:
                return
    except Exception:
        return

    # Range
    try:
        earliest = non_null.min()
        latest = non_null.max()
        span_days = (latest - earliest).days
        metrics["earliest"] = str(earliest)
        metrics["latest"] = str(latest)
        metrics["span_days"] = int(span_days)
        findings.append(f"Range: {earliest} to {latest} ({span_days} days)")
    except Exception:
        pass

    # Gaps
    try:
        sorted_vals = non_null.sort_values()
        diffs = sorted_vals.diff().dropna()
        if len(diffs) > 0:
            max_gap = diffs.max()
            metrics["max_gap_days"] = round(max_gap.total_seconds() / 86400, 2)
            if metrics["max_gap_days"] > 30:
                findings.append(f"Largest gap: {metrics['max_gap_days']:.1f} days")
    except Exception:
        pass

    # Cadence inference
    try:
        if len(diffs) > 0:
            median_gap_hours = diffs.median().total_seconds() / 3600
            if median_gap_hours < 0.1:
                cadence = "sub-minute"
            elif median_gap_hours < 2:
                cadence = "hourly"
            elif median_gap_hours < 36:
                cadence = "daily"
            elif median_gap_hours < 200:
                cadence = "weekly"
            elif median_gap_hours < 1000:
                cadence = "monthly"
            else:
                cadence = "irregular"
            metrics["inferred_cadence"] = cadence
            findings.append(f"Inferred cadence: {cadence}")
    except Exception:
        pass

    # Day-of-week distribution
    try:
        dow = non_null.dt.dayofweek.value_counts().sort_index()
        dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow_dist = []
        for day_num, count in dow.items():
            dow_dist.append({"day": dow_names[int(day_num)], "count": int(count)})
        samples["day_of_week_distribution"] = dow_dist
    except Exception:
        pass

    # Future dates
    try:
        now = pd.Timestamp.now()
        future_count = int((non_null > now).sum())
        if future_count > 0:
            metrics["future_date_count"] = future_count
            findings.append(f"{future_count} future dates detected — data quality flag")
            risks.append(f"{future_count} dates are in the future — may indicate data errors")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Private: Boolean analysis
# ---------------------------------------------------------------------------


def _analyze_boolean(
    series: pd.Series,
    metrics: dict,
    findings: list,
    risks: list,
    samples: dict,
) -> None:
    """Deep analysis for boolean/flag columns."""
    row_count = len(series)
    null_count = int(series.isna().sum())

    # Count true/false
    non_null = series.dropna()
    if len(non_null) == 0:
        return

    try:
        # Normalize to boolean-like
        str_vals = non_null.astype(str).str.lower()
        true_set = {"true", "1", "yes", "y"}
        false_set = {"false", "0", "no", "n"}

        true_count = int(str_vals.isin(true_set).sum())
        false_count = int(str_vals.isin(false_set).sum())

        metrics["true_count"] = true_count
        metrics["false_count"] = false_count
        metrics["null_count_bool"] = null_count
        metrics["true_pct"] = round(true_count / row_count, 4) if row_count > 0 else 0.0
        metrics["false_pct"] = round(false_count / row_count, 4) if row_count > 0 else 0.0

        # Balance ratio
        total_valid = true_count + false_count
        if total_valid > 0:
            balance = min(true_count, false_count) / total_valid
            metrics["balance_ratio"] = round(balance, 4)
            if balance < 0.1:
                findings.append(f"Highly skewed: {balance:.1%} balance ratio")
                findings.append(
                    f"Distribution: True={true_count} ({true_count/row_count:.0%}), "
                    f"False={false_count} ({false_count/row_count:.0%}), "
                    f"Null={null_count} ({null_count/row_count:.0%})"
                )
            else:
                findings.append(
                    f"Distribution: True={true_count} ({true_count/row_count:.0%}), "
                    f"False={false_count} ({false_count/row_count:.0%}), "
                    f"Null={null_count} ({null_count/row_count:.0%})"
                )

        samples["boolean_distribution"] = {
            "true": true_count,
            "false": false_count,
            "null": null_count,
        }
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Private: Helpers
# ---------------------------------------------------------------------------


def _build_summary(column: str, col_type: str, spark_type: str, metrics: dict) -> str:
    """Build one-line summary string."""
    parts = [
        spark_type.upper(),
        f"{metrics.get('row_count', 0):,} values",
        f"{metrics.get('null_pct', 0):.1%} null",
        f"{metrics.get('distinct_count', 0):,} distinct ({metrics.get('distinct_pct', 0):.1%})",
    ]
    if metrics.get("role"):
        parts.append(f"role={metrics['role']}")
    if metrics.get("semantic_type"):
        parts.append(f"type={metrics['semantic_type']}")
    return " | ".join(parts)


def _build_next_actions(column: str, col_type: str, metrics: dict, findings: list, risks: list) -> list[str]:
    """Build suggested next actions based on analysis."""
    actions = []

    if metrics.get("null_like_count", 0) > 0:
        actions.append(f"Clean null-like values: COALESCE(NULLIF(TRIM({column}), ''), NULL)")

    if metrics.get("whitespace_issues", 0) > 0:
        actions.append(f"Trim whitespace: TRIM({column})")

    if metrics.get("outlier_count", 0) > 0:
        actions.append(
            f"Run anchor('case_file', df, column='{column}', filter='outliers') to see affected rows"
        )

    if metrics.get("null_pct", 0) > 0.05:
        actions.append(
            f"Run anchor('case_file', df, column='{column}', filter='nulls') to investigate null pattern"
        )

    if metrics.get("future_date_count", 0) > 0:
        actions.append(
            f"Run anchor('case_file', df, column='{column}', filter='where:{column} > current_date()') "
            f"to investigate future dates"
        )

    if not actions:
        actions.append("Column looks clean — no immediate actions needed")

    return actions
