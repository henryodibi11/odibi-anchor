#!/usr/bin/env python
"""Verify zero forbidden framework imports remain in odibi_anchor source and tests.

Checks for both legacy (datakit) and current (odibi) framework imports.
odibi_anchor must remain self-contained with no external framework deps.

Exit code 0 = clean, 1 = violations found.
Usage: python scripts/check_no_datakit_imports.py
"""
import ast
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = [
    os.path.join(REPO_ROOT, "src"),
    os.path.join(REPO_ROOT, "tests"),
]

FORBIDDEN_ROOTS = {"datakit", "odibi"}


def check_file(fpath: str) -> list[tuple[int, str]]:
    """Return real forbidden import nodes without matching comments or strings."""
    with open(fpath, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        tree = ast.parse(source, filename=fpath)
    except SyntaxError as exc:
        return [(exc.lineno or 1, f"SYNTAX ERROR: {exc.msg}")]

    lines = source.splitlines()
    violations = []
    for node in ast.walk(tree):
        imported = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported = [node.module]
        if any(name.split(".", 1)[0] in FORBIDDEN_ROOTS for name in imported):
            line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else repr(node)
            violations.append((node.lineno, line))
    return sorted(violations)


def main():
    """Check that no source files import from external framework packages."""
    total_violations = 0
    files_scanned = 0

    print("=" * 60)
    print("CHECK: No forbidden framework imports in odibi_anchor")
    print("=" * 60)

    for scan_dir in SCAN_DIRS:
        for root, _, files in os.walk(scan_dir):
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                files_scanned += 1
                violations = check_file(fpath)
                if violations:
                    rel = os.path.relpath(fpath, REPO_ROOT)
                    for lineno, text in violations:
                        print(f"  VIOLATION: {rel}:{lineno}: {text}")
                        total_violations += 1

    print(f"\nScanned: {files_scanned} files")
    if total_violations == 0:
        print("\n✅ PASS — Zero forbidden framework imports found")
        return 0
    else:
        print(f"\n❌ FAIL — {total_violations} forbidden framework import(s) found")
        return 1


if __name__ == "__main__":
    sys.exit(main())
