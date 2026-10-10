"""Regenerate the released host-guidance byte table from release tags.

``anchor setup-host --reconcile`` classifies a managed file as ``released_version``
when its SHA-256 equals bytes that a released Anchor version installed. This script
derives that table from every ``vMAJOR.MINOR.PATCH`` tag in a full Git clone:

    python scripts/generate_released_guidance_hashes.py --write

Run it after tagging a release so the next release recognizes the tagged bytes.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src" / "odibi_anchor" / "_released_guidance_hashes.json"
_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True
    ).stdout


def _tags() -> list[tuple[tuple[int, int, int], str]]:
    tags = []
    for line in _git("tag", "--list", "v*").decode().split():
        match = _TAG.fullmatch(line)
        if match:
            tags.append((tuple(int(part) for part in match.groups()), line))
    return sorted(tags)


def _pointers(tag: str) -> dict[str, bytes]:
    try:
        source = _git("show", f"{tag}:src/odibi_anchor/host_setup.py").decode()
    except subprocess.CalledProcessError:
        return {}
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_POINTERS" for target in node.targets)
        ):
            value = ast.literal_eval(node.value)
            return {name: content for name, content in value.items() if isinstance(content, bytes)}
    return {}


def _released_files(tag: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    listing = _git(
        "ls-tree", "-r", "--name-only", tag, "--", ".assistant", ".assistant_instructions.md",
        "agent_bootstrap.py",
    ).decode().splitlines()
    for relative in listing:
        parts = relative.split("/")
        if "__pycache__" in parts or relative.endswith((".pyc", ".pyo")):
            continue
        files[relative] = _git("show", f"{tag}:{relative}")
    files.update(_pointers(tag))
    return files


def generate() -> dict[str, object]:
    table: dict[str, dict[str, list[str]]] = {}
    for _version, tag in _tags():
        version = tag.removeprefix("v")
        for relative, content in _released_files(tag).items():
            digest = hashlib.sha256(content).hexdigest()
            span = table.setdefault(relative, {}).setdefault(digest, [version, version])
            span[1] = version
    return {
        "format": "odibi-anchor-released-guidance-hashes-v1",
        "description": (
            "SHA-256 of host-guidance bytes installed by released Anchor versions; each "
            "digest maps to the first and last release tag that contained those bytes."
        ),
        "files": {path: dict(sorted(digests.items())) for path, digests in sorted(table.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help=f"write {OUTPUT.relative_to(ROOT)}")
    args = parser.parse_args()
    if _git("rev-parse", "--is-shallow-repository").strip() == b"true":
        print("refusing to generate from a shallow clone; run git fetch --unshallow", file=sys.stderr)
        return 2
    rendered = json.dumps(generate(), indent=1, sort_keys=True) + "\n"
    if args.write:
        OUTPUT.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
