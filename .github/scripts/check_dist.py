"""Fail closed when release archives contain state or incorrect metadata."""
from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED_VERSION = "0.1.0"
FORBIDDEN = re.compile(
    r"(^|/)(?:\.git|\.venv|\.pytest_cache|\.ruff_cache|\.context-workbench|"
    r"\.odibi-anchor|workspace|sessions|__pycache__)(?:/|$)|"
    r"(^|/)(?:\.env(?:\..*)?|\.anchor[^/]*|.*\.(?:py[co]|db(?:-.*)?|sqlite(?:3)?(?:-.*)?|tmp))$"
)


def names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def check_path(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe archive path: {name}")
    if FORBIDDEN.search(name):
        raise SystemExit(f"forbidden runtime/private path: {name}")


def main() -> None:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    artifacts = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    expected = {
        f"odibi_anchor-{EXPECTED_VERSION}-py3-none-any.whl",
        f"odibi_anchor-{EXPECTED_VERSION}.tar.gz",
    }
    if {path.name for path in artifacts} != expected:
        raise SystemExit(f"expected exactly {sorted(expected)}, found {[p.name for p in artifacts]}")
    for artifact in artifacts:
        entries = names(artifact)
        for entry in entries:
            check_path(entry)
        print(f"checked {artifact.name}: {len(entries)} entries")


if __name__ == "__main__":
    main()
