from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_external_amp_guide_has_portable_safety_contract() -> None:
    guide = (ROOT / "docs/guides/amp-project-integration.md").read_text(encoding="utf-8")
    for contract in (
        "odibi-anchor[mcp]==0.3.0",
        "<full-commit-sha>",
        "never install a mutable branch",
        "ANCHOR_PROJECT_ROOT",
        "ANCHOR_HOME",
        "outside that checkout",
        "AGENTS.md",
    ):
        assert contract in guide
