"""Shared utilities for table_profiler tools.

Internal module — not part of the public API.
"""

from __future__ import annotations

import sys
from typing import Any

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True


# ---------------------------------------------------------------------------
# Null-like sentinels (re-exported from cleanliness for internal use)
# ---------------------------------------------------------------------------

from .cleanliness import _NULL_LIKE as NULL_LIKE_SENTINELS


# ---------------------------------------------------------------------------
# Pattern fingerprinting
# ---------------------------------------------------------------------------


def fingerprint_value(value: str) -> str:
    """Convert a string value to a pattern fingerprint.

    e.g. 'ABC-123' -> 'AAA-999', 'hello world' -> 'aaaaa aaaaa'
    """
    result = []
    for ch in str(value):
        if ch.isdigit():
            result.append("9")
        elif ch.isupper():
            result.append("A")
        elif ch.islower():
            result.append("a")
        else:
            result.append(ch)
    return "".join(result)


# ---------------------------------------------------------------------------
# JSON-safe value conversion
# ---------------------------------------------------------------------------


def to_json_safe(val: Any) -> Any:
    """Convert a value to a JSON-serializable representation.

    Handles numpy scalars, pandas Timestamps, NaT, Inf, and NaN.
    This is the single canonical implementation — all tools should use this.
    """
    if val is None:
        return None
    if isinstance(val, float) and (np.isnan(val) or np.isinf(val)):
        return None
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if isinstance(val, pd.Timestamp):
        return str(val)
    if val is pd.NaT:
        return None
    return val


def serialize_dict_safe(d: dict) -> dict:
    """Ensure all values in a flat dict are JSON-serializable."""
    return {k: to_json_safe(v) for k, v in d.items()}


def serialize_list_safe(items: list) -> list:
    """Deep-serialize a list of dicts/values for JSON safety."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append({k: to_json_safe(v) for k, v in item.items()})
        else:
            result.append(to_json_safe(item))
    return result


def serialize_samples_safe(samples: dict) -> dict:
    """Deep-serialize a samples dict for JSON safety."""
    result = {}
    for k, v in samples.items():
        if isinstance(v, list):
            result[k] = serialize_list_safe(v)
        elif isinstance(v, dict):
            result[k] = serialize_dict_safe(v)
        else:
            result[k] = to_json_safe(v)
    return result
