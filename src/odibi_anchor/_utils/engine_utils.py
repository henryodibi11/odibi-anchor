"""Canonical engine detection for odibi_anchor.

Provides a single source of truth for detecting whether a DataFrame
is Spark, Pandas, or unknown. All modules should import from here
rather than implementing their own detection logic.

Usage:
    from odibi_anchor._utils.engine_utils import is_spark_df, is_pandas_df, detect_engine

    if is_spark_df(df):
        # Spark path
    else:
        # Pandas path
"""

from typing import Any


def is_spark_df(df: Any) -> bool:
    """Check if df is a PySpark DataFrame.

    Uses isinstance when pyspark is available, falls back to
    module-name heuristics to avoid hard dependency.

    Args:
        df: Object to check.

    Returns:
        True if df is a PySpark DataFrame.
    """
    try:
        from pyspark.sql import DataFrame as SparkDF
        return isinstance(df, SparkDF)
    except ImportError:
        pass

    # Fallback: check module path without importing pyspark
    mod = getattr(type(df), "__module__", "") or ""
    return mod.startswith("pyspark.sql")


def is_pandas_df(df: Any) -> bool:
    """Check if df is a Pandas DataFrame.

    Args:
        df: Object to check.

    Returns:
        True if df is a pandas DataFrame.
    """
    try:
        import pandas as pd
        return isinstance(df, pd.DataFrame)
    except ImportError:
        pass

    mod = getattr(type(df), "__module__", "") or ""
    return "pandas.core.frame" in mod


def detect_engine(df: Any) -> str:
    """Detect the engine for a DataFrame-like object.

    Args:
        df: Object to inspect.

    Returns:
        "spark", "pandas", or "unknown".

    Example:
        >>> import pandas as pd
        >>> detect_engine(pd.DataFrame({"a": [1]}))
        'pandas'
    """
    if is_spark_df(df):
        return "spark"
    if is_pandas_df(df):
        return "pandas"
    return "unknown"
