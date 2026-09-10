"""Tests for odibi_anchor._utils.contract module."""

import pandas as pd
import pytest

import odibi_anchor._utils.contract as contract_module
from odibi_anchor._utils.contract import (
    ContractViolation,
    build_base_context,
    finalize_context,
    guard_dataframe_type,
    validate_output_format,
)


# ---------------------------------------------------------------------------
# validate_output_format
# ---------------------------------------------------------------------------


class TestValidateOutputFormat:
    """Tests for validate_output_format()."""

    def test_dict_is_valid(self):
        validate_output_format("dict")  # Should not raise

    def test_markdown_is_valid(self):
        validate_output_format("markdown")  # Should not raise

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError, match="output_format must be"):
            validate_output_format("json")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            validate_output_format("")

    def test_none_raises(self):
        with pytest.raises((ValueError, TypeError)):
            validate_output_format(None)

    def test_case_sensitive(self):
        with pytest.raises(ValueError):
            validate_output_format("Dict")

    def test_case_sensitive_markdown(self):
        with pytest.raises(ValueError):
            validate_output_format("Markdown")


# ---------------------------------------------------------------------------
# build_base_context
# ---------------------------------------------------------------------------


class TestGuardDataFrameType:
    """Tests for guard_dataframe_type()."""

    def test_accepts_pandas_dataframe(self):
        guard_dataframe_type(pd.DataFrame({"x": [1]}))

    def test_accepts_detected_spark_dataframe(self, monkeypatch):
        class FakeSparkDataFrame:
            pass

        monkeypatch.setattr(contract_module, "detect_engine", lambda df: "spark")
        guard_dataframe_type(FakeSparkDataFrame())

    def test_none_raises_type_error(self):
        with pytest.raises(TypeError, match="df must be a Spark or Pandas DataFrame"):
            guard_dataframe_type(None)

    def test_custom_param_name_in_error(self):
        with pytest.raises(TypeError, match="left_df must be a Spark or Pandas DataFrame"):
            guard_dataframe_type("not a dataframe", param_name="left_df")


# ---------------------------------------------------------------------------
# build_base_context
# ---------------------------------------------------------------------------


class TestBuildBaseContext:
    """Tests for build_base_context()."""

    def test_returns_dict_with_standard_keys(self):
        ctx = build_base_context(
            kind="test_tool",
            subject="my_table",
            summary="All good.",
            metrics={"rows": 100},
        )
        assert ctx["kind"] == "test_tool"
        assert ctx["subject"] == "my_table"
        assert ctx["summary"] == "All good."
        assert ctx["metrics"] == {"rows": 100}
        assert ctx["findings"] == []
        assert ctx["risks"] == []
        assert ctx["samples"] == {}
        assert ctx["suggested_next_actions"] == []

    def test_populates_standard_fields_from_kwargs(self):
        ctx = build_base_context(
            kind="k",
            subject="s",
            summary="sum",
            metrics={},
            findings=["f1", "f2"],
            risks=["r1"],
            samples={"evidence": [{"a": 1}]},
            suggested_next_actions=["act1"],
        )
        assert ctx["findings"] == ["f1", "f2"]
        assert ctx["risks"] == ["r1"]
        assert ctx["samples"] == {"evidence": [{"a": 1}]}
        assert ctx["suggested_next_actions"] == ["act1"]

    def test_extra_kwargs_become_top_level_keys(self):
        ctx = build_base_context(
            kind="k",
            subject="s",
            summary="sum",
            metrics={},
            custom_field="hello",
            another=42,
        )
        assert ctx["custom_field"] == "hello"
        assert ctx["another"] == 42

    def test_extra_kwargs_do_not_overwrite_standard_keys(self):
        ctx = build_base_context(
            kind="k",
            subject="s",
            summary="sum",
            metrics={"x": 1},
            findings=["real"],
        )
        # findings should be the passed value, not default
        assert ctx["findings"] == ["real"]

    def test_metrics_is_preserved_as_dict(self):
        m = {"a": 1, "b": 2.5, "c": True}
        ctx = build_base_context(kind="k", subject="s", summary="ok", metrics=m)
        assert ctx["metrics"] is m


# ---------------------------------------------------------------------------
# Runtime contract enforcement
# ---------------------------------------------------------------------------


class TestContractEnforcement:
    """Tests that build_base_context raises ContractViolation on bad types."""

    def test_empty_kind_raises(self):
        with pytest.raises(ContractViolation, match="kind must be a non-empty str"):
            build_base_context(kind="", subject="s", summary="sum", metrics={})

    def test_none_kind_raises(self):
        with pytest.raises(ContractViolation, match="kind must be a non-empty str"):
            build_base_context(kind=None, subject="s", summary="sum", metrics={})

    def test_non_str_subject_raises(self):
        with pytest.raises(ContractViolation, match="subject must be str"):
            build_base_context(kind="k", subject=123, summary="sum", metrics={})

    def test_empty_summary_raises(self):
        with pytest.raises(ContractViolation, match="summary must be a non-empty str"):
            build_base_context(kind="k", subject="s", summary="", metrics={})

    def test_non_dict_metrics_raises(self):
        with pytest.raises(ContractViolation, match="metrics must be dict"):
            build_base_context(kind="k", subject="s", summary="sum", metrics=[1, 2])

    def test_non_list_findings_raises(self):
        with pytest.raises(ContractViolation, match="findings must be list"):
            build_base_context(kind="k", subject="s", summary="sum", metrics={},
                             findings="not a list")

    def test_non_list_risks_raises(self):
        with pytest.raises(ContractViolation, match="risks must be list"):
            build_base_context(kind="k", subject="s", summary="sum", metrics={},
                             risks="not a list")

    def test_non_dict_samples_raises(self):
        with pytest.raises(ContractViolation, match="samples must be dict"):
            build_base_context(kind="k", subject="s", summary="sum", metrics={},
                             samples=[{"a": 1}])

    def test_non_list_suggested_next_actions_raises(self):
        with pytest.raises(ContractViolation, match="suggested_next_actions must be list"):
            build_base_context(kind="k", subject="s", summary="sum", metrics={},
                             suggested_next_actions="not a list")

    def test_valid_call_does_not_raise(self):
        """Sanity check that valid inputs pass all checks."""
        ctx = build_base_context(
            kind="tool",
            subject="data",
            summary="All good.",
            metrics={"n": 1},
            findings=["found something"],
            risks=["watch out"],
            samples={"preview": [{"col": "val"}]},
            suggested_next_actions=["MUST: verify"],
        )
        assert ctx["kind"] == "tool"


# ---------------------------------------------------------------------------
# finalize_context
# ---------------------------------------------------------------------------


class TestFinalizeContext:
    """Tests for finalize_context()."""

    def test_returns_dict_when_format_is_dict(self):
        ctx = {"kind": "test", "summary": "ok"}
        result = finalize_context(ctx, "dict", lambda c: "md output")
        assert result is ctx

    def test_calls_render_fn_when_format_is_markdown(self):
        ctx = {"kind": "test", "summary": "ok"}
        result = finalize_context(ctx, "markdown", lambda c: f"# {c['kind']}")
        assert result == "# test"

    def test_render_fn_receives_ctx(self):
        received = []

        def capture(c):
            received.append(c)
            return ""

        ctx = {"kind": "x"}
        finalize_context(ctx, "markdown", capture)
        assert received == [ctx]

    def test_dict_format_does_not_call_render(self):
        called = []
        finalize_context({}, "dict", lambda c: called.append(1) or "")
        assert called == []
