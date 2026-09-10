"""Tests for lib/_common.py shared utilities."""
import sys
sys.dont_write_bytecode = True

import json
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, "/Workspace/Users/user@example.com/tools/table_profiler")
from tools.table_profiler_tool.lib._common import fingerprint_value, to_json_safe, serialize_dict_safe


class TestFingerprintValue:
    def test_alpha_digits(self):
        assert fingerprint_value("ABC-123") == "AAA-999"

    def test_lowercase(self):
        assert fingerprint_value("hello") == "aaaaa"

    def test_mixed(self):
        assert fingerprint_value("Order#42") == "Aaaaa#99"

    def test_special_chars_preserved(self):
        assert fingerprint_value("A-B_C") == "A-A_A"

    def test_empty_string(self):
        assert fingerprint_value("") == ""


class TestToJsonSafe:
    def test_none(self):
        assert to_json_safe(None) is None

    def test_nan(self):
        assert to_json_safe(float("nan")) is None

    def test_inf(self):
        assert to_json_safe(float("inf")) is None

    def test_neg_inf(self):
        assert to_json_safe(float("-inf")) is None

    def test_numpy_int(self):
        result = to_json_safe(np.int64(42))
        assert result == 42
        assert type(result) is int

    def test_numpy_float(self):
        result = to_json_safe(np.float64(3.14))
        assert result == 3.14
        assert type(result) is float

    def test_numpy_bool(self):
        result = to_json_safe(np.bool_(True))
        assert result is True
        assert type(result) is bool

    def test_pandas_timestamp(self):
        ts = pd.Timestamp("2024-01-15 10:30:00")
        assert to_json_safe(ts) == "2024-01-15 10:30:00"

    def test_pandas_nat(self):
        assert to_json_safe(pd.NaT) is None

    def test_regular_values_pass_through(self):
        assert to_json_safe(42) == 42
        assert to_json_safe("hello") == "hello"
        assert to_json_safe(3.14) == 3.14
        assert to_json_safe(True) is True

    def test_result_is_json_serializable(self):
        """Output of to_json_safe should always be JSON-serializable."""
        values = [np.int64(1), np.float64(2.5), np.bool_(False), pd.NaT, float("nan"), None]
        for v in values:
            safe = to_json_safe(v)
            json.dumps(safe)  # Should not raise


class TestSerializeDictSafe:
    def test_basic(self):
        d = {"a": np.int64(1), "b": float("nan"), "c": "hello"}
        result = serialize_dict_safe(d)
        assert result["a"] == 1
        assert result["b"] is None
        assert result["c"] == "hello"

    def test_result_is_json_serializable(self):
        d = {"x": np.float64(1.5), "y": pd.NaT}
        result = serialize_dict_safe(d)
        json.dumps(result)  # Should not raise
