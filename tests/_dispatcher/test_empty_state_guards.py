"""AXI #5: definitive empty-state messaging on the investigation builders."""
import pandas as pd

from odibi_anchor._utils.contract import (
    is_empty_df, empty_df_context, render_empty_df_md,
)
from odibi_anchor._dispatcher._tool_wrappers import (
    _profile_table_context, _microscope_context, _case_file_context,
)


def test_is_empty_df_detection():
    assert is_empty_df(pd.DataFrame({"a": []})) is True
    assert is_empty_df(pd.DataFrame({"a": [1]})) is False
    assert is_empty_df([]) is False          # list, not a DataFrame
    assert is_empty_df({}) is False          # dict, not a DataFrame
    assert is_empty_df(None) is False


def test_empty_df_context_shape():
    ctx = empty_df_context("profile_table_context", "t", "profile_table", columns=["a", "b"])
    assert ctx["metrics"]["row_count"] == 0
    assert ctx["metrics"]["column_count"] == 2
    assert "EMPTY-INPUT" in " ".join(ctx["findings"])
    assert ctx["suggested_next_actions"]


def test_render_empty_df_md():
    ctx = empty_df_context("microscope", "c", "microscope", columns=["c"])
    md = render_empty_df_md(ctx)
    assert md.startswith("# microscope")
    assert "0 rows" in md


def test_profile_table_empty_guard_dict():
    df = pd.DataFrame({"id": pd.Series([], dtype="int64")})
    r = _profile_table_context(df, subject="t", output_format="dict")
    assert r["kind"] == "profile_table_context"
    assert r["metrics"]["row_count"] == 0


def test_profile_table_empty_guard_markdown():
    df = pd.DataFrame({"id": pd.Series([], dtype="int64")})
    r = _profile_table_context(df, subject="t", output_format="markdown")
    assert isinstance(r, str)
    assert "0 rows" in r


def test_microscope_empty_guard():
    df = pd.DataFrame({"c": pd.Series([], dtype="object")})
    r = _microscope_context(df, "c", output_format="dict")
    assert r["kind"] == "microscope"
    assert r["metrics"]["row_count"] == 0


def test_case_file_empty_guard():
    df = pd.DataFrame({"c": pd.Series([], dtype="object")})
    r = _case_file_context(df, column="c", filter="nulls", output_format="dict")
    assert r["kind"] == "case_file"
    assert r["metrics"]["row_count"] == 0


def test_nonempty_input_does_not_trigger_guard():
    df = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    r = _profile_table_context(df, subject="t", output_format="dict")
    assert "cannot analyze an empty input" not in str(r)
