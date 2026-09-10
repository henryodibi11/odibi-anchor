"""Tests for codebase_map_context detail='summary' (AXI minimal default schema)."""
import json
from pathlib import Path

import pytest

# Import via the package (not the full submodule path): codebase/__init__ re-exports
# the function, and a full-path import would rebind the package attribute to the
# SUBMODULE, shadowing the function for other tests ('module' object is not callable).
from odibi_anchor.codebase import codebase_map_context

_REPO = Path(__file__).resolve().parents[2]
_SUB = str(_REPO / "src" / "odibi_anchor" / "profiling")
_PKG = str(_REPO / "src" / "odibi_anchor")


def test_summary_modules_are_lean_name_strings():
    d = codebase_map_context(_SUB, detail="summary", output_format="dict")
    for m in d["modules"].values():
        assert all(isinstance(f, str) for f in m["functions"])
        assert all(isinstance(c, str) for c in m["classes"])
        assert "n_imports" in m


def test_full_modules_remain_detailed_by_default():
    d = codebase_map_context(_SUB, output_format="dict")
    assert any(isinstance(f, dict) for m in d["modules"].values() for f in m["functions"])


def test_summary_is_substantially_smaller():
    full = codebase_map_context(_PKG, output_format="dict")
    summ = codebase_map_context(_PKG, detail="summary", output_format="dict")
    assert len(json.dumps(summ)) < len(json.dumps(full)) * 0.6


def test_summary_markdown_renders_without_crash():
    md = codebase_map_context(_SUB, detail="summary", output_format="markdown")
    assert md.startswith("# Codebase Map")


def test_invalid_detail_raises():
    with pytest.raises(ValueError):
        codebase_map_context(_SUB, detail="bogus")
