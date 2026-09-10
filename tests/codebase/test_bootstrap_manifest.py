"""Integration test: bootstrap.init manifest wiring (single source of truth)."""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


def test_bootstrap_loads_manifest():
    """Verify init() returns a dict MANIFEST after bootstrap."""
    project_root = str(Path(__file__).resolve().parents[2])
    script = f"""
import sys, os
sys.path.insert(0, os.path.join({project_root!r}, "src"))
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init(root={project_root!r})
assert isinstance(MANIFEST, dict), f"MANIFEST should be dict, got {{type(MANIFEST)}}"
print("PASS: MANIFEST is dict")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, f"Bootstrap failed: {result.stderr[-500:]}"
    assert "PASS: MANIFEST is dict" in result.stdout


def test_manifest_in_dispatch():
    """Verify the 'manifest' action is wired in the dispatcher source."""
    project_root = Path(__file__).resolve().parents[2]
    source = (project_root / "src" / "odibi_anchor" / "bootstrap.py").read_text(encoding="utf-8")
    assert '"manifest":' in source
    assert "_manifest_mod.manifest_context" in source
    assert "MANIFEST" in source
