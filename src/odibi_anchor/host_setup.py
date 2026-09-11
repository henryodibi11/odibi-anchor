"""Idempotent installation of packaged guidance into an explicit host root."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

_MANIFEST = ".odibi-anchor-host-guidance.json"
_MANIFEST_VERSION = 1
_ADAPTER_FILES = {
    "amp": "AGENTS.md",
    "claude": "CLAUDE.md",
    "databricks": "agent_bootstrap.py",
    "chatgpt": "AGENTS.md",
}
_ADAPTER_OMITTED_PREFIXES = {
    # Workspace Files cannot reliably materialize the deeply nested third-party
    # snapshot cache. The installed package retains it for runtime reference
    # lookup; host guidance needs the canonical contract, skills, and authored
    # references only.
    "databricks": (".assistant/references/snapshots/",),
}
_POINTERS = {
    "AGENTS.md": (
        b"# Odibi Anchor guidance\n\n"
        b"Read and follow the canonical `.assistant_instructions.md` contract and its "
        b"companion `.assistant/` resources before substantive work.\n"
    ),
    "CLAUDE.md": (
        b"# Odibi Anchor guidance\n\n"
        b"Read and follow the canonical `.assistant_instructions.md` contract and its "
        b"companion `.assistant/` resources before substantive work.\n"
    ),
}


class HostSetupError(RuntimeError):
    """A fail-closed reconciliation refusal with stable diagnostics."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _target_root(value: str | os.PathLike[str]) -> Path:
    text = os.fspath(value)
    path = Path(text)
    if not text or "\n" in text or "\r" in text or text.startswith("~") or not path.is_absolute():
        raise ValueError("target_root must be an explicit absolute, single-line path")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("target_root must be an existing absolute directory") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("target_root must be a real existing directory, not a symlink")
    return path.resolve()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise HostSetupError("host guidance manifest is malformed: invalid managed path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HostSetupError("host guidance manifest is malformed: invalid managed path")
    return relative.as_posix()


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostSetupError(f"{label} is missing or inaccessible") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise HostSetupError(f"{label} must be a regular file (symlinks are refused)")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HostSetupError(f"{label} is missing or inaccessible") from exc


def _desired_files(adapter: str) -> dict[str, bytes]:
    from odibi_anchor._runtime_paths import resolve_resource_root

    resources = resolve_resource_root()
    assistant = resources / ".assistant"
    instructions = resources / ".assistant_instructions.md"
    if not assistant.is_dir() or assistant.is_symlink():
        raise HostSetupError("installed distribution is missing regular .assistant resources")
    desired: dict[str, bytes] = {
        ".assistant_instructions.md": _regular_bytes(
            instructions, "packaged .assistant_instructions.md"
        )
    }
    for candidate in sorted(assistant.rglob("*")):
        if candidate.is_symlink():
            raise HostSetupError(
                f"packaged guidance contains a symlink: {candidate.relative_to(resources).as_posix()}"
            )
        if candidate.is_file():
            relative = candidate.relative_to(resources).as_posix()
            relative_parts = PurePosixPath(relative).parts
            if "__pycache__" in relative_parts or relative.endswith((".pyc", ".pyo")):
                continue
            if relative.startswith(_ADAPTER_OMITTED_PREFIXES.get(adapter, ())):
                continue
            desired[relative] = _regular_bytes(candidate, f"packaged {relative}")
    host_file = _ADAPTER_FILES[adapter]
    if host_file == "agent_bootstrap.py":
        desired[host_file] = _regular_bytes(
            resources / host_file, "packaged agent_bootstrap.py"
        )
    else:
        desired[host_file] = _POINTERS[host_file]
    return dict(sorted(desired.items()))


def _load_manifest(target: Path) -> dict[str, Any] | None:
    path = target / _MANIFEST
    if not path.exists():
        return None
    content = _regular_bytes(path, f"managed manifest {_MANIFEST}")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostSetupError("host guidance manifest is malformed: invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("version") != _MANIFEST_VERSION
        or value.get("adapter") not in _ADAPTER_FILES
        or not isinstance(value.get("files"), dict)
        or set(value) != {"version", "adapter", "files"}
    ):
        raise HostSetupError("host guidance manifest is malformed: unsupported schema")
    files: dict[str, str] = {}
    for raw_path, digest in value["files"].items():
        relative = _safe_relative(raw_path)
        if relative == _MANIFEST or not isinstance(digest, str) or len(digest) != 64:
            raise HostSetupError("host guidance manifest is malformed: invalid managed entry")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise HostSetupError("host guidance manifest is malformed: invalid managed hash") from exc
        files[relative] = digest
    if len(files) != len(value["files"]):
        raise HostSetupError("host guidance manifest is malformed: duplicate managed path")
    normalized = {"version": _MANIFEST_VERSION, "adapter": value["adapter"], "files": files}
    canonical = (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode()
    if content != canonical:
        raise HostSetupError("host guidance manifest was modified or is non-canonical")
    return normalized


def _destination(target: Path, relative: str) -> Path:
    destination = target.joinpath(*PurePosixPath(relative).parts)
    current = target
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise HostSetupError(f"unsafe destination path contains symlink: {relative}")
        if current.exists() and not current.is_dir():
            raise HostSetupError(f"unsafe destination parent is not a directory: {relative}")
    return destination


def setup_host(
    target_root: str | os.PathLike[str], *, adapter: str
) -> dict[str, Any]:
    """Reconcile packaged host guidance without overwriting unowned or edited files.

    ``target_root`` is mandatory and never inferred.  All collisions are validated
    before publication; a manifest records the exact bytes owned by Anchor.
    """
    target = _target_root(target_root)
    if adapter not in _ADAPTER_FILES:
        raise ValueError("adapter must be one of: amp, chatgpt, claude, databricks")
    desired = _desired_files(adapter)
    manifest = _load_manifest(target)
    if manifest is not None and manifest["adapter"] != adapter:
        raise HostSetupError(
            f"host guidance manifest adapter mismatch: managed={manifest['adapter']}, requested={adapter}"
        )
    previous: dict[str, str] = {} if manifest is None else manifest["files"]
    compatible_unmanaged: list[str] = []
    host_file = _ADAPTER_FILES[adapter]
    host_destination = target / host_file
    if host_file not in previous and host_file in desired and host_destination.exists():
        existing_host = _regular_bytes(host_destination, f"destination {host_file}")
        if (
            host_file in {"AGENTS.md", "CLAUDE.md"}
            and b".assistant_instructions.md" in existing_host
        ):
            desired.pop(host_file)
            compatible_unmanaged.append(host_file)

    for relative in sorted(set(previous) | set(desired)):
        destination = _destination(target, relative)
        expected = previous.get(relative)
        if destination.exists() or destination.is_symlink():
            actual = _sha256(_regular_bytes(destination, f"destination {relative}"))
            if expected is None:
                if relative in desired and actual == _sha256(desired[relative]):
                    continue
                raise HostSetupError(f"unmanaged destination collision: {relative}")
            if actual != expected:
                raise HostSetupError(
                    f"modified managed file: {relative} (expected {expected}, actual {actual})"
                )
        elif expected is not None:
            raise HostSetupError(f"modified managed file: {relative} (expected {expected}, actual missing)")

    hashes = {relative: _sha256(content) for relative, content in desired.items()}
    manifest_bytes = (json.dumps(
        {"version": _MANIFEST_VERSION, "adapter": adapter, "files": hashes},
        indent=2, sort_keys=True,
    ) + "\n").encode()
    unchanged = manifest is not None and previous == hashes
    if unchanged:
        return _result(target, adapter, "unchanged", hashes, compatible_unmanaged)

    staging = Path(tempfile.mkdtemp(prefix=".anchor-host-stage-", dir=target))
    backup = staging / "backup"
    published: list[tuple[Path, Path | None]] = []
    preserve_staging = False
    try:
        for relative, content in desired.items():
            staged = staging / "new" / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
        staged_manifest = staging / "new" / _MANIFEST
        staged_manifest.write_bytes(manifest_bytes)
        for relative in sorted(set(previous) - set(desired)):
            destination = _destination(target, relative)
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            preserve_staging = True
            os.replace(destination, saved)
            published.append((destination, saved))
        for relative in [*desired, _MANIFEST]:
            destination = _destination(target, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            saved: Path | None = None
            if destination.exists():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                preserve_staging = True
                os.replace(destination, saved)
            published.append((destination, saved))
            os.replace(staging / "new" / relative, destination)
        preserve_staging = False
    except BaseException as publication_error:
        preserve_staging = True
        tracked_backups = {
            saved for _destination_path, saved in published if saved is not None
        }
        if backup.is_dir():
            for saved in sorted(path for path in backup.rglob("*") if path.is_file()):
                if saved not in tracked_backups:
                    published.append(
                        (_destination(target, saved.relative_to(backup).as_posix()), saved)
                    )
        rollback_errors: list[str] = []
        for destination, saved in reversed(published):
            try:
                destination.unlink(missing_ok=True)
                if saved is not None and saved.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(saved, destination)
            except BaseException as exc:
                rollback_errors.append(f"{destination}: {type(exc).__name__}")
        remaining_backups = (
            sorted(path for path in backup.rglob("*") if path.is_file())
            if backup.is_dir()
            else []
        )
        if remaining_backups:
            rollback_errors.append(
                f"{len(remaining_backups)} recovery backup(s) remain"
            )
        if rollback_errors:
            raise HostSetupError(
                "host setup publication and rollback failed; backups preserved at "
                f"{staging}: {', '.join(rollback_errors)}"
            ) from publication_error
        preserve_staging = False
        raise
    finally:
        if not preserve_staging:
            shutil.rmtree(staging, ignore_errors=True)
    status = "installed" if manifest is None else "upgraded"
    return _result(target, adapter, status, hashes, compatible_unmanaged)


def _result(
    target: Path,
    adapter: str,
    status: str,
    hashes: dict[str, str],
    compatible_unmanaged: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "host_guidance_setup",
        "status": status,
        "adapter": adapter,
        "resource_profile": (
            "databricks_workspace_compact" if adapter == "databricks" else "complete"
        ),
        "omitted_packaged_prefixes": list(_ADAPTER_OMITTED_PREFIXES.get(adapter, ())),
        "target_root": str(target),
        "manifest_path": str(target / _MANIFEST),
        "managed_files": [
            {"path": relative, "sha256": digest} for relative, digest in sorted(hashes.items())
        ],
        "compatible_unmanaged_files": sorted(compatible_unmanaged or []),
        "next_operation": {
            "operation": "review_host_guidance",
            "path": str(target / _ADAPTER_FILES[adapter]),
        },
    }


__all__ = ["HostSetupError", "setup_host"]
