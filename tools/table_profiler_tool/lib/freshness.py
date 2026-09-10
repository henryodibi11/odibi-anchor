"""Freshness and cadence detection for Table Profiler.

Identifies the best temporal column in a DataFrame, measures data staleness,
detects load cadence (hourly/daily/weekly/monthly/irregular), and flags
missing time periods.

Public API
----------
detect_freshness(df, profiles=None) -> FreshnessAnalysis | None
"""

from __future__ import annotations

import re
import sys
from typing import Any, cast

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from .models import ColumnProfile, ColumnRole, FreshnessAnalysis, Inference

try:
    from pyspark.sql import DataFrame as SparkDataFrame  # noqa: F401
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# spark_type keywords that identify a temporal column
_TEMPORAL_SPARK_TYPES = frozenset({"timestamp", "date", "timestamp_ntz", "datetime64"})

# Cadence boundary thresholds (in seconds)
_HOURLY_MAX_S = 3_600 + 900           # 1h 15m
_DAILY_MAX_S = 86_400 + 7_200         # 26 hours
_WEEKLY_MAX_S = 7 * 86_400 + 86_400   # 8 days
_MONTHLY_MAX_S = 31 * 86_400           # 31 days

# Minimum fraction of non-null values required to accept a string column as temporal
_MIN_PARSE_RATE = 0.80

# Column name patterns that indicate an audit/load timestamp (ETL provenance).
# These are preferred over business-date columns when multiple temporal cols exist
# because they reflect when data *arrived*, not when the business event occurred.
_AUDIT_NAME_RE = re.compile(
    r"(?:^|_)(?:"
    r"extracted|loaded|load_date|ingested|ingestion|inserted|insert_date"
    r"|created_at|created_timestamp|updated_at|updated_timestamp"
    r"|processed_at|process_date|etl|batch_date|run_date|run_timestamp"
    r"|source_timestamp|file_created|file_modified"
    r")(?:$|_)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_freshness(
    df: Any,
    profiles: list[ColumnProfile] | None = None,
) -> FreshnessAnalysis | None:
    """Detect freshness, staleness, and load cadence from a DataFrame.

    Strategy:
    1. Identify the best temporal column (type -> role -> parse).
    2. Extract latest / earliest values and compute staleness vs UTC now.
    3. Detect cadence from the median inter-arrival interval.
    4. Flag missing periods (any gap > 2x the median interval).

    Args:
        df: Input pandas or Spark DataFrame.
        profiles: Pre-computed ColumnProfile list (avoids re-scanning schema).

    Returns:
        FreshnessAnalysis if a temporal column is found, else None.
    """
    engine = detect_engine(df)
    col_name = _find_temporal_column(df, engine, profiles)
    if col_name is None:
        return None

    if engine == "pandas":
        return _analyze_pandas(df, col_name)
    return _analyze_spark(df, col_name)


# ---------------------------------------------------------------------------
# Temporal column detection
# ---------------------------------------------------------------------------


def _find_temporal_column(
    df: Any,
    engine: str,
    profiles: list[ColumnProfile] | None,
) -> str | None:
    """Return the best temporal column name, or None if none found.

    Priority:
    1. Profile spark_type contains a known temporal keyword.
    2. Profile role == ColumnRole.TIMESTAMP.
    3. String column where >= _MIN_PARSE_RATE of non-null values parse as datetime.
    """
    if profiles:
        # Priority 1: typed temporal — prefer audit/load-named columns.
        # Collect all typed temporal candidates and score by name:
        # audit-named columns (ETL provenance) are ranked above business dates.
        typed_temporal = [
            p.name for p in profiles
            if any(
                t in p.spark_type.lower().split("[")[0].strip()
                for t in _TEMPORAL_SPARK_TYPES
            )
        ]
        if typed_temporal:
            return _pick_preferred(typed_temporal)
        # Priority 2: role-tagged
        for p in profiles:
            if p.role == ColumnRole.TIMESTAMP:
                return p.name

    # No profiles: inspect the DataFrame directly
    if engine == "pandas":
        # Priority 3: columns that are already a datetime dtype — prefer audit-named.
        datetime_cols = [
            col for col in df.columns
            if pd.api.types.is_datetime64_any_dtype(df[col])
        ]
        if datetime_cols:
            return _pick_preferred(datetime_cols)
        # Priority 4: string columns where values parse as datetime
        return _detect_by_parsing_pandas(df)

    # Spark without profiles: inspect schema field types — prefer audit-named.
    try:
        spark_temporal = [
            field.name for field in df.schema.fields
            if any(t in type(field.dataType).__name__.lower() for t in _TEMPORAL_SPARK_TYPES)
        ]
        if spark_temporal:
            return _pick_preferred(spark_temporal)
    except Exception:  # noqa: BLE001
        pass
    return None


def _pick_preferred(candidates: list[str]) -> str:
    """Return the best temporal column from a list of candidates.

    Audit/load-named columns (matching _AUDIT_NAME_RE) are ranked first because
    they reflect data *arrival* time, which is more meaningful for freshness
    monitoring than business-event dates.  When no audit col is present the
    first candidate in schema order is returned.
    """
    for name in candidates:
        if _AUDIT_NAME_RE.search(name):
            return name
    return candidates[0]


def _detect_by_parsing_pandas(df: pd.DataFrame) -> str | None:
    """Return first string column where >= _MIN_PARSE_RATE of values parse as datetime."""
    for col in df.columns:
        if not pd.api.types.is_object_dtype(df[col]):
            continue
        non_null = df[col].dropna()
        if len(non_null) == 0:
            continue
        try:
            parsed = pd.to_datetime(non_null, utc=True, errors="coerce")
            parse_rate = parsed.notna().sum() / len(non_null)
            if parse_rate >= _MIN_PARSE_RATE:
                return col
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# Pandas analysis path
# ---------------------------------------------------------------------------


def _analyze_pandas(df: pd.DataFrame, col_name: str) -> FreshnessAnalysis | None:
    """Run full freshness analysis on a pandas DataFrame."""
    raw = pd.to_datetime(df[col_name], utc=True, errors="coerce")
    series = raw.dropna().sort_values().reset_index(drop=True)

    if series.empty:
        return None  # All-null temporal column

    now = pd.Timestamp.now(tz="UTC")
    latest_ts = series.iloc[-1]
    earliest_ts = series.iloc[0]

    staleness_hours = max(0.0, (now - latest_ts).total_seconds() / 3600)
    staleness = _format_staleness(staleness_hours)

    cadence, median_s = _detect_cadence(series)
    gap_detected, gap_description = _detect_gap(series, median_s)
    avg_rows = _avg_rows_per_period(df, col_name, cadence)
    last_load = _last_load_count(df, col_name, latest_ts, median_s)

    confidence = 0.9 if cadence and cadence != "irregular" else 0.7

    return FreshnessAnalysis(
        freshness_column=col_name,
        latest_value=str(latest_ts),
        earliest_value=str(earliest_ts),
        staleness=staleness,
        staleness_hours=round(staleness_hours, 2),
        cadence=cadence,
        avg_rows_per_period=avg_rows,
        last_load_row_count=last_load,
        gap_detected=gap_detected,
        gap_description=gap_description,
        inference=Inference(
            value="freshness_detected",
            confidence=confidence,
            evidence=[
                f"freshness_column={col_name}",
                f"latest={latest_ts}",
                f"staleness_hours={staleness_hours:.2f}",
                f"cadence={cadence}",
            ],
            sample_size=len(df),
            method="freshness_detection",
        ),
    )


def _detect_cadence(series: pd.Series) -> tuple[str | None, float]:
    """Return (cadence_label, median_interval_seconds) from a sorted timestamp series.

    Returns (None, 0.0) when fewer than 2 non-null values are present.
    """
    if len(series) < 2:
        return None, 0.0

    diffs = series.diff().dropna()
    if diffs.empty:
        return None, 0.0

    median_s = float(diffs.dt.total_seconds().median())
    if median_s <= 0:
        return "irregular", 0.0

    if median_s <= _HOURLY_MAX_S:
        label = "hourly"
    elif median_s <= _DAILY_MAX_S:
        label = "daily"
    elif median_s <= _WEEKLY_MAX_S:
        label = "weekly"
    elif median_s <= _MONTHLY_MAX_S:
        label = "monthly"
    else:
        label = "irregular"

    return label, median_s


def _detect_gap(
    series: pd.Series,
    median_s: float,
) -> tuple[bool, str | None]:
    """Return (gap_detected, gap_description) for the largest gap in the series.

    A gap is any consecutive interval greater than 2x the median interval.
    gap_description names the boundaries of the largest detected gap.
    """
    if len(series) < 2 or median_s <= 0:
        return False, None

    diffs_s = series.diff().dropna().dt.total_seconds()
    if not (diffs_s > 2 * median_s).any():
        return False, None

    # Largest gap: diff at label max_idx = series[max_idx] - series[max_idx - 1]
    max_idx = int(diffs_s.idxmax())
    gap_start = series.iloc[max_idx - 1]
    gap_end = series.iloc[max_idx]
    desc = f"No data between {gap_start.date()} and {gap_end.date()}"
    return True, desc


def _avg_rows_per_period(
    df: pd.DataFrame,
    col_name: str,
    cadence: str | None,
) -> float | None:
    """Return mean row count per cadence bucket, or None for irregular/unknown cadence."""
    if cadence is None or cadence == "irregular":
        return None

    try:
        ts = pd.to_datetime(df[col_name], utc=True, errors="coerce").dropna()
        if ts.empty:
            return None

        if cadence == "hourly":
            buckets = ts.dt.floor("h")
        elif cadence == "daily":
            buckets = ts.dt.floor("D")
        else:
            # weekly / monthly: to_period() requires tz-naive
            ts_naive = ts.dt.tz_convert(None)
            freq = "W" if cadence == "weekly" else "M"
            buckets = ts_naive.dt.to_period(freq).astype(str)

        counts = buckets.value_counts()
        return round(float(counts.mean()), 2)
    except Exception:  # noqa: BLE001
        return None


def _last_load_count(
    df: pd.DataFrame,
    col_name: str,
    latest_ts: pd.Timestamp,
    median_s: float,
) -> int | None:
    """Count rows falling within one median interval of the latest timestamp."""
    if median_s <= 0:
        return None
    window_start = latest_ts - pd.Timedelta(seconds=median_s)
    try:
        ts = pd.to_datetime(df[col_name], utc=True, errors="coerce")
        return int((ts >= window_start).sum())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Spark analysis path
# ---------------------------------------------------------------------------


def _analyze_spark(df: Any, col_name: str) -> FreshnessAnalysis | None:
    """Run freshness analysis on a Spark DataFrame.

    Uses native Spark aggregation for max/min/count; collects a 1000-row sample
    of timestamps for cadence and gap detection.
    """
    if F is None:
        raise TypeError("PySpark is not available for Spark freshness detection.")

    ts_col = F.col(col_name).cast("timestamp")
    agg = df.agg(
        F.max(ts_col).alias("latest"),
        F.min(ts_col).alias("earliest"),
        F.count(ts_col).alias("n"),
    ).collect()[0]

    if agg["latest"] is None:
        return None

    def _to_utc(ts: Any) -> pd.Timestamp:
        p: pd.Timestamp = cast(pd.Timestamp, pd.Timestamp(ts))
        return p.tz_localize("UTC") if p.tzinfo is None else p.tz_convert("UTC")

    latest_ts = _to_utc(agg["latest"])
    earliest_ts = _to_utc(agg["earliest"])
    row_count = int(agg["n"])

    now = pd.Timestamp.now(tz="UTC")
    staleness_hours = max(0.0, (now - latest_ts).total_seconds() / 3600)

    # Sample timestamps for cadence/gap analysis
    sample_pd = (
        df.select(F.col(col_name).cast("timestamp").alias("_ts"))
        .where(F.col("_ts").isNotNull())
        .orderBy(F.col("_ts").asc())
        .limit(1000)
        .toPandas()
    )
    series = (
        pd.to_datetime(sample_pd["_ts"], utc=True, errors="coerce")
        .dropna()
        .sort_values()
        .reset_index(drop=True)
    )
    cadence, median_s = _detect_cadence(series)
    gap_detected, gap_description = _detect_gap(series, median_s)
    confidence = 0.9 if cadence and cadence != "irregular" else 0.7

    return FreshnessAnalysis(
        freshness_column=col_name,
        latest_value=str(latest_ts),
        earliest_value=str(earliest_ts),
        staleness=_format_staleness(staleness_hours),
        staleness_hours=round(staleness_hours, 2),
        cadence=cadence,
        avg_rows_per_period=None,  # Expensive on Spark — deferred
        last_load_row_count=row_count,
        gap_detected=gap_detected,
        gap_description=gap_description,
        inference=Inference(
            value="freshness_detected",
            confidence=confidence,
            evidence=[
                f"freshness_column={col_name}",
                f"latest={latest_ts}",
                f"staleness_hours={staleness_hours:.2f}",
                f"cadence={cadence}",
            ],
            sample_size=row_count,
            method="freshness_detection",
        ),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_staleness(hours: float) -> str:
    """Format a staleness duration as a human-readable string.

    Examples: "23.5m ago", "3.2h ago", "2.1d ago".
    """
    if hours < 1.0:
        return f"{hours * 60:.1f}m ago"
    if hours < 24.0:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"
