"""Refresh licensed, pinned offline source snapshots for engineering references.

Run from the repository root with network access. Consumers never need network access.
The script downloads archives by immutable commit, copies only declared documentation/API
paths, retains licenses and notices, and writes a hash manifest for every distributed file.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / ".assistant" / "references" / "snapshots"


@dataclass(frozen=True)
class Snapshot:
    name: str
    repository: str
    revision: str
    version: str
    license: str
    include: tuple[str, ...]

    @property
    def archive_url(self) -> str:
        return f"https://github.com/{self.repository}/archive/{self.revision}.tar.gz"


SNAPSHOTS = (
    Snapshot(
        "vega-6.3.1", "vega/vega", "c42b7bad10730383c780f7769768b8b64bc0052a",
        "6.3.1", "BSD-3-Clause",
        ("LICENSE", "docs/vega-schema.json", "docs/docs/**/*.md"),
    ),
    Snapshot(
        "vega-lite-6.4.3", "vega/vega-lite", "f4bb2188709204860329aff2aeaf678c0280c315",
        "6.4.3", "BSD-3-Clause",
        ("LICENSE", "build/vega-lite-schema.json", "site/docs/**/*.md"),
    ),
    Snapshot(
        "altair-6.2.2", "vega/altair", "a9765713566095349cb1cfbbe85d6ad258c84245",
        "6.2.2", "BSD-3-Clause",
        (
            "LICENSE", "doc/index.rst", "doc/getting_started/**/*.rst",
            "doc/user_guide/**/*.rst", "doc/user_guide/**/*.py",
            "altair/vegalite/api.py", "altair/vegalite/schema.py",
            "altair/vegalite/v6/schema/vega-lite-schema.json",
            "altair/vegalite/v6/schema/core.py", "altair/vegalite/v6/schema/channels.py",
            "altair/vegalite/v6/schema/mixins.py",
        ),
    ),
    Snapshot(
        "vl-convert-1.9.0", "vega/vl-convert", "655a7579017b9f52cf67b9631cc5def17cf92fae",
        "1.9.0", "BSD-3-Clause",
        (
            "LICENSE", "README.md", "thirdparty_javascript.md", "thirdparty_font.md",
            "vl-convert-python/README.md", "vl-convert-python/vl_convert.pyi",
        ),
    ),
    Snapshot(
        "pydantic-2.13.4", "pydantic/pydantic", "cf67d4b3193c3fe43ede18612ed62785eee11382",
        "2.13.4", "MIT",
        (
            "LICENSE", "docs/migration.md", "docs/api/*.md", "docs/api/**/*.md",
            "docs/concepts/*.md", "docs/concepts/**/*.md", "docs/errors/*.md",
            "docs/errors/**/*.md", "docs/internals/*.md", "docs/internals/**/*.md",
        ),
    ),
    Snapshot(
        "deneb-1.9.1", "deneb-viz/deneb", "0615a47dc20b7e03d6d5143b1d045aaa47e0b508",
        "1.9.1", "MIT",
        (
            "LICENSE", "README.md", "CHANGELOG.md", "capabilities.json", "pbiviz.json",
            "packages/vega-runtime/src/lib/embed/index.ts",
        ),
    ),
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not members:
        raise RuntimeError("empty source archive")
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe source archive member: {member.name}")
    return members


def _download(snapshot: Snapshot) -> tuple[bytes, str]:
    request = urllib.request.Request(snapshot.archive_url, headers={"User-Agent": "odibi-anchor"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(100_000_001)
    if len(data) > 100_000_000:
        raise RuntimeError(f"source archive exceeds 100 MB: {snapshot.name}")
    return data, hashlib.sha256(data).hexdigest()


def _copy_snapshot(snapshot: Snapshot, archive_bytes: bytes, temporary: Path) -> list[dict[str, object]]:
    output = temporary / snapshot.name
    output.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        members = _safe_members(archive)
        prefix = Path(members[0].name).parts[0]
        selected = []
        for member in members:
            parts = Path(member.name).parts
            if not member.isfile() or not parts or parts[0] != prefix:
                continue
            relative = Path(*parts[1:]).as_posix()
            if _matches(relative, snapshot.include):
                selected.append((member, relative))
        if not selected:
            raise RuntimeError(f"no files selected for {snapshot.name}")
        for member, relative in selected:
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read {member.name}")
            # Keep source references inert inside wheels: installers may otherwise compile
            # distributed .py snapshots and create forbidden __pycache__ directories.
            distributed_relative = f"{relative}.txt" if relative.endswith(".py") else relative
            target = output / distributed_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files.append({
                "path": path.relative_to(temporary).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    return files


def main() -> None:
    """Replace snapshots atomically after every pinned source validates."""
    with tempfile.TemporaryDirectory(prefix="anchor-reference-snapshots-") as value:
        temporary = Path(value)
        records = []
        for snapshot in SNAPSHOTS:
            archive, archive_sha256 = _download(snapshot)
            files = _copy_snapshot(snapshot, archive, temporary)
            records.append({
                "name": snapshot.name,
                "repository": snapshot.repository,
                "revision": snapshot.revision,
                "version": snapshot.version,
                "license": snapshot.license,
                "archive_url": snapshot.archive_url,
                "archive_sha256": archive_sha256,
                "files": files,
            })
        manifest = {
            "schema_version": 1,
            "generated_on": "2026-08-14",
            "policy": "Pinned licensed source documentation and API artifacts for offline use.",
            "snapshots": records,
            "deneb_public_docs": {
                "status": "not-copied",
                "reason": "The documentation repository has no explicit LICENSE file; the MIT-licensed application source and an original synthesis are retained instead.",
                "source_revision": "00d6bc6f261f324905a78e26fe2b512cabfbe9ed",
            },
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        backup = DESTINATION.with_name(f"{DESTINATION.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if DESTINATION.exists():
            DESTINATION.rename(backup)
        try:
            shutil.copytree(temporary, DESTINATION)
        except Exception:
            if DESTINATION.exists():
                shutil.rmtree(DESTINATION)
            if backup.exists():
                backup.rename(DESTINATION)
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup)


if __name__ == "__main__":
    main()
