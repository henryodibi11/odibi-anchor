#!/usr/bin/env python
"""Canonical command-line pytest launcher for odibi_anchor."""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from odibi_anchor.pytest_runner import run_pytest  # noqa: E402


def main():
    """Forward every command-line argument unchanged and print one final summary."""
    args = sys.argv[1:] or ["tests/"]
    summary, _ = run_pytest(args, cwd=REPO_ROOT)
    print(json.dumps(summary, sort_keys=True))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
