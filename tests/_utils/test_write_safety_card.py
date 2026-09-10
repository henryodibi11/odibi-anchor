"""W-1: write-safety card attached to quality output + the renderer."""
import pandas as pd

from odibi_anchor._utils._write_safety import render_write_safety_card
from odibi_anchor.validation.quality_gate_context import quality_gate_context


def test_renderer_safe():
    card = render_write_safety_card(safe=True, headline="safe to write",
                                    signals=["checks: 5/5 passed"])
    assert "SAFE" in card and "❌" not in card


def test_renderer_blocked_includes_fix():
    card = render_write_safety_card(safe=False, headline="do NOT write",
                                    signals=["failed: duplicate_keys"], fix_hint="fix it")
    assert "BLOCKED" in card and "→ fix it" in card


def test_quality_blocked_has_card():
    df = pd.DataFrame({"id": [1, 1, 2], "v": [10, 20, 30]})  # dup key
    ctx = quality_gate_context(df, keys=["id"], subject="t", output_format="dict")
    assert "write_safety_card" in ctx
    assert ctx["metrics"]["verdict"] == "BLOCKED"
    assert "BLOCKED" in ctx["write_safety_card"]


def test_quality_safe_has_card():
    df = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
    ctx = quality_gate_context(df, keys=["id"], subject="t", output_format="dict")
    assert ctx["metrics"]["verdict"] == "SAFE"
    assert "SAFE" in ctx["write_safety_card"]
