"""Tests for the selective TOON encoder (_utils/_toon.py)."""
import json

from odibi_anchor._utils._toon import render_toon


def test_uniform_array_of_flat_dicts_becomes_table():
    out = render_toon({"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})
    assert "rows[2]{a,b}:" in out
    assert "1,2" in out and "3,4" in out


def test_dict_of_flat_dicts_becomes_keyed_table():
    out = render_toon({"files": {"x.py": {"lines": 10, "hash": "a"},
                                 "y.py": {"lines": 20, "hash": "b"}}})
    assert "files[2]{_key,lines,hash}:" in out
    assert "x.py,10,a" in out and "y.py,20,b" in out


def test_scalars_and_scalar_lists():
    out = render_toon({"k": "v", "n": 3, "tags": ["a", "b"]})
    assert "k: v" in out
    assert "n: 3" in out
    assert "tags[2]: a, b" in out


def test_nested_nonuniform_falls_back_to_compact_json():
    # Non-uniform array (differing key sets) is TOON's weak spot → compact JSON.
    out = render_toon({"weird": [{"a": 1}, {"a": 1, "b": 2}]})
    line = [l for l in out.splitlines() if l.startswith("weird:")][0]
    assert json.loads(line[len("weird: "):]) == [{"a": 1}, {"a": 1, "b": 2}]


def test_comma_or_whitespace_values_are_quoted():
    out = render_toon({"rows": [{"a": "x,y"}, {"a": " pad"}]})
    assert '"x,y"' in out
    assert '" pad"' in out


def test_empty_list_renders_with_zero_marker():
    out = render_toon({"items": []})
    assert "items[0]:" in out


def test_non_dict_input_is_json():
    assert render_toon([1, 2, 3]) == "[1,2,3]"
