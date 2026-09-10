"""Shared sampling helpers for Table Profiler.

Provides deterministic pandas and Spark sampling utilities with a hard safety cap
on collected values so string analysis cannot accidentally collect entire columns.
"""

from __future__ import annotations

import random
import sys
from collections.abc import Sequence
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

MAX_COLLECTED_ROWS_PER_COLUMN = 5000
DEFAULT_SAMPLE_SIZE = 1000
DEFAULT_SAMPLE_SEED = 42

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover - pyspark optional for pandas-only tests
    SparkDataFrame = None
    F = None


try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy expected but keep import optional
    np = None


def detect_engine(df: Any) -> str:
    """Return the execution engine name for the provided DataFrame."""

    if isinstance(df, pd.DataFrame):
        return "pandas"
    if SparkDataFrame is not None and isinstance(df, SparkDataFrame):
        return "spark"
    raise TypeError(f"Unsupported DataFrame type: {type(df)!r}")


def clamp_sample_size(n: int) -> int:
    """Clamp a requested sample size to the project-wide hard safety cap."""

    if n <= 0:
        raise ValueError("Sample size must be greater than zero.")
    return min(n, MAX_COLLECTED_ROWS_PER_COLUMN)


def get_sample(
    df: Any,
    column: str,
    n: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SAMPLE_SEED,
) -> list[Any]:
    """Return a deterministic sample of non-null column values.

    Args:
        df: Input pandas or Spark DataFrame.
        column: Column to sample.
        n: Requested sample size before clamping to the hard cap.
        seed: Deterministic seed used by pandas and Spark sampling.

    Returns:
        List of sampled scalar values from the requested column.

    Raises:
        KeyError: If the requested column does not exist.
        TypeError: If df is neither pandas nor Spark.
        ValueError: If the requested sample size is invalid.
    """

    engine = detect_engine(df)
    sample_size = clamp_sample_size(n)

    if engine == "pandas":
        return _get_sample_pandas(df, column, sample_size, seed)
    return _get_sample_spark(df, column, sample_size, seed)


def sample_strings(
    df: Any,
    column: str,
    n: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SAMPLE_SEED,
) -> list[str]:
    """Return sampled column values converted to strings for pattern analysis."""

    sampled_values = get_sample(df, column=column, n=n, seed=seed)
    return [str(value) for value in sampled_values if value is not None]


def _get_sample_pandas(
    df: pd.DataFrame,
    column: str,
    n: int,
    seed: int,
) -> list[Any]:
    """Return a deterministic sample from a pandas DataFrame column."""

    if column not in df.columns:
        raise KeyError(f"Column not found: {column}")

    non_null = df[column].dropna()
    if non_null.empty:
        return []

    sample_size = min(n, len(non_null))
    sampled = non_null.sample(n=sample_size, random_state=seed, replace=False)
    return sampled.tolist()


def _get_sample_spark(
    df: Any,
    column: str,
    n: int,
    seed: int,
) -> list[Any]:
    """Return a bounded deterministic sample from a Spark DataFrame column."""

    if F is None or SparkDataFrame is None:
        raise TypeError("PySpark is not available for Spark sampling.")
    if column not in df.columns:
        raise KeyError(f"Column not found: {column}")

    non_null_df = df.select(column).where(F.col(column).isNotNull())
    sampled_rows = (
        non_null_df.orderBy(F.rand(seed))
        .limit(n)
        .collect()
    )
    return [row[column] for row in sampled_rows]


def representative_sample(
    values: Sequence[Any],
    n: int = 5,
    seed: int = DEFAULT_SAMPLE_SEED,
) -> list[Any]:
    """Return a small deterministic sample from an in-memory sequence."""

    sample_size = clamp_sample_size(n)
    normalized_values = [value for value in values if value is not None]
    if not normalized_values:
        return []

    if len(normalized_values) <= sample_size:
        return list(normalized_values)

    if np is not None and len(normalized_values) >= 3:
        quantiles = np.linspace(0, len(normalized_values) - 1, sample_size, dtype=int)
        return [normalized_values[index] for index in quantiles]

    rng = random.Random(seed)
    return rng.sample(normalized_values, sample_size)
