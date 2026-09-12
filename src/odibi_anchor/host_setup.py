"""Idempotent installation of packaged guidance into an explicit host root."""
from __future__ import annotations

import hashlib
import importlib
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
_CUSTOM_INSTRUCTIONS = ".assistant_instructions.md"
_LEGACY_MANAGED_HASHES = {
    ".assistant/agent_bootstrap.py": {
        "8dfc8b467e29df34c5a48c8d9b3357c5ccd67c5ad93aad42297bd2d3adb536ad",
        "8ed4de2f506731bb583e37c0d7e917f982eb31715f40ce5eaa66f1a6ebb11245",
    },
    ".assistant/references/odibi-anchor/quick-reference.md": {
        "d482abaabf9d17dd33b94cfd76b939aff7cebf82ac37d2b14a04c2128a0782d7",
        "14dc0b1398ad9d921e1d8af262ba664db8f5ead2674fe3d89fb5caa14fd51175",
    },
    ".assistant/references/odibi-anchor/workflow.md": {
        "55c70b2e684073928147c2b34668fe1e51d0bac558f9a764c43390aa1756f01a",
        "fe89de603ffa3d4b0a2a36ac54c8f21d366053d8dd5e60dcb0266d209487a0ae",
    },
    ".assistant/skills/setting-up-odibi-anchor/SKILL.md": {
        "4df690526e5e99389e3a8d3c7343f428d4bc134015f226bb5c82827ea7c5efb9",
        "7060ddeaff3d6dd1d584959516e434a157fc3f7baf7eab625d0bb568980a8674",
        "fa6347999fd21cf2438854700e6d64da849682802f088c135ee19c3da3a229df",
    },
    _CUSTOM_INSTRUCTIONS: {
        "ee43a7cb557143b34d804ff7d147495396d07e6e2b3d12f50efdc377abe03743",
        "1762931aa39bc3c79368100eebb16b1041826d3e802753343bb2c53acdc5cf55",
        "45b35fc067069afd17dcc2a8b67ff2511eeb1dca851b1369316d0ea9000af5a7",
    },
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


def _databricks_target_root(value: str | os.PathLike[str]) -> Path | None:
    text = os.fspath(value)
    if not text.startswith("/Workspace/"):
        return None
    path = PurePosixPath(text)
    if (
        not text
        or "\n" in text
        or "\r" in text
        or text.endswith("/")
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("target_root must be an explicit normalized Databricks Workspace path")
    return Path(text)


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


def _verify_publication(target: Path, desired: dict[str, bytes], manifest_bytes: bytes) -> None:
    """Prove every published byte before reporting host setup success."""
    for relative, expected in desired.items():
        actual = _regular_bytes(_destination(target, relative), f"published {relative}")
        if actual != expected:
            raise HostSetupError(f"published host guidance verification failed: {relative}")
    if _regular_bytes(target / _MANIFEST, f"published {_MANIFEST}") != manifest_bytes:
        raise HostSetupError("published host guidance verification failed: manifest")


def _cleanup_staging(staging: Path, adapter: str) -> None:
    """Remove local staging, using the Workspace API for Databricks FUSE residue."""
    failures: list[str] = []

    def record_failure(function: Any, path: str, _exc_info: Any) -> None:
        failures.append(f"{getattr(function, '__name__', type(function).__name__)}:{path}")

    shutil.rmtree(staging, onerror=record_failure)
    if not failures and not staging.exists():
        return
    raw_path = staging.as_posix()
    if adapter != "databricks" or not raw_path.startswith("/Workspace/"):
        detail = ", ".join(failures) if failures else "staging path remains"
        raise HostSetupError(f"host guidance published but staging cleanup failed: {detail}")
    try:
        sdk = importlib.import_module("databricks.sdk")
        client = sdk.WorkspaceClient()
        client.workspace.delete(path=raw_path.removeprefix("/Workspace"), recursive=True)
    except Exception as exc:
        raise HostSetupError(
            f"host guidance published but Workspace staging cleanup failed: {staging}"
        ) from exc


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
            content = _regular_bytes(candidate, f"packaged {relative}")
            desired[relative] = content
            if adapter == "claude" and relative.startswith(".assistant/skills/"):
                desired[".claude/skills/" + relative.removeprefix(".assistant/skills/")] = content
    host_file = _ADAPTER_FILES[adapter]
    if host_file == "agent_bootstrap.py":
        desired[host_file] = _regular_bytes(
            resources / host_file, "packaged agent_bootstrap.py"
        )
    else:
        desired[host_file] = _POINTERS[host_file]
    return dict(sorted(desired.items()))


def _parse_manifest(content: bytes) -> dict[str, Any]:
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


def _load_manifest(target: Path) -> dict[str, Any] | None:
    path = target / _MANIFEST
    if not path.exists():
        return None
    return _parse_manifest(_regular_bytes(path, f"managed manifest {_MANIFEST}"))


def _legacy_reconciliation(
    desired: dict[str, bytes], existing: dict[str, bytes | None],
) -> tuple[dict[str, bytes], dict[str, str], list[str], list[str]]:
    """Recognize released Anchor bytes when an older host has no ownership manifest."""
    legacy: dict[str, str] = {}
    unknown: list[str] = []
    for relative, expected in desired.items():
        content = existing.get(relative)
        if content is None or content == expected:
            continue
        digest = _sha256(content)
        if digest in _LEGACY_MANAGED_HASHES.get(relative, set()):
            legacy[relative] = digest
        else:
            unknown.append(relative)
    if not legacy:
        return desired, {}, [], []
    compatible: list[str] = []
    reconciled = dict(desired)
    if unknown == [_CUSTOM_INSTRUCTIONS]:
        content = existing[_CUSTOM_INSTRUCTIONS]
        assert content is not None
        if b"# Odibi Anchor operating contract" in content and b"agent_bootstrap.py" in content:
            reconciled.pop(_CUSTOM_INSTRUCTIONS)
            compatible.append(_CUSTOM_INSTRUCTIONS)
            unknown.clear()
    if unknown:
        return desired, {}, [], []
    return reconciled, legacy, compatible, sorted(legacy)


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


def _workspace_api_path(target: Path, relative: str | None = None) -> str:
    root = target.as_posix().removeprefix("/Workspace")
    if relative is None:
        return root
    return f"{root}/{relative}"


def _workspace_missing(exc: BaseException) -> bool:
    return (
        isinstance(exc, FileNotFoundError)
        or getattr(exc, "status_code", None) == 404
        or str(getattr(exc, "error_code", "")).upper()
        in {"NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"}
    )


def _workspace_read(workspace: Any, path: str) -> bytes | None:
    try:
        stream = workspace.download(path)
    except Exception as exc:
        if _workspace_missing(exc):
            return None
        raise HostSetupError(f"Workspace guidance path is inaccessible: {path}") from exc
    try:
        content = stream.read()
    except Exception as exc:
        raise HostSetupError(f"Workspace guidance path is unreadable: {path}") from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    if not isinstance(content, bytes):
        raise HostSetupError(f"Workspace guidance path did not return bytes: {path}")
    return content


def _workspace_write(workspace: Any, path: str, content: bytes, import_format: Any) -> None:
    workspace.mkdirs(PurePosixPath(path).parent.as_posix())
    workspace.upload(path, content, format=import_format, overwrite=True)


def _workspace_delete(workspace: Any, path: str) -> None:
    try:
        workspace.delete(path)
    except Exception as exc:
        if not _workspace_missing(exc):
            raise


def _verify_workspace_publication(
    workspace: Any,
    target: Path,
    desired: dict[str, bytes],
    manifest_bytes: bytes,
) -> None:
    for relative, expected in desired.items():
        if _workspace_read(workspace, _workspace_api_path(target, relative)) != expected:
            raise HostSetupError(f"published host guidance verification failed: {relative}")
    if _workspace_read(workspace, _workspace_api_path(target, _MANIFEST)) != manifest_bytes:
        raise HostSetupError("published host guidance verification failed: manifest")


def _setup_databricks_workspace(
    target: Path,
    desired: dict[str, bytes],
) -> dict[str, Any]:
    try:
        sdk = importlib.import_module("databricks.sdk")
        workspace_types = importlib.import_module("databricks.sdk.service.workspace")
        workspace = sdk.WorkspaceClient().workspace
        import_format = workspace_types.ImportFormat.AUTO
    except Exception as exc:
        raise HostSetupError("Databricks Workspace host setup requires an authenticated SDK") from exc

    target_path = _workspace_api_path(target)
    try:
        target_status = workspace.get_status(target_path)
    except Exception as exc:
        raise HostSetupError(f"Databricks Workspace target is inaccessible: {target_path}") from exc
    raw_object_type = getattr(target_status, "object_type", None)
    object_type = str(getattr(raw_object_type, "value", raw_object_type)).upper()
    if object_type not in {"DIRECTORY", "REPO"}:
        raise HostSetupError("Databricks Workspace target must be a directory or Git Folder")

    manifest_path = _workspace_api_path(target, _MANIFEST)
    manifest_content = _workspace_read(workspace, manifest_path)
    manifest = None if manifest_content is None else _parse_manifest(manifest_content)
    if manifest is not None and manifest["adapter"] != "databricks":
        raise HostSetupError(
            "host guidance manifest adapter mismatch: "
            f"managed={manifest['adapter']}, requested=databricks"
        )
    previous: dict[str, str] = {} if manifest is None else manifest["files"]
    compatible_unmanaged: list[str] = []
    existing: dict[str, bytes | None] = {
        relative: _workspace_read(workspace, _workspace_api_path(target, relative))
        for relative in desired
    }
    legacy_managed: list[str] = []
    if manifest is None:
        desired, previous, compatible_unmanaged, legacy_managed = _legacy_reconciliation(
            desired, existing
        )
    elif _CUSTOM_INSTRUCTIONS not in previous:
        custom = existing.get(_CUSTOM_INSTRUCTIONS)
        if (
            custom is not None
            and b"# Odibi Anchor operating contract" in custom
            and b"agent_bootstrap.py" in custom
        ):
            desired.pop(_CUSTOM_INSTRUCTIONS)
            compatible_unmanaged.append(_CUSTOM_INSTRUCTIONS)
    for relative in sorted(set(previous) | set(desired)):
        content = existing.get(relative)
        if relative not in existing:
            content = _workspace_read(workspace, _workspace_api_path(target, relative))
        existing[relative] = content
        expected = previous.get(relative)
        if content is not None:
            actual = _sha256(content)
            if expected is None:
                if relative in desired and actual == _sha256(desired[relative]):
                    continue
                raise HostSetupError(f"unmanaged destination collision: {relative}")
            if actual != expected:
                raise HostSetupError(
                    f"modified managed file: {relative} (expected {expected}, actual {actual})"
                )
        elif expected is not None:
            raise HostSetupError(
                f"modified managed file: {relative} (expected {expected}, actual missing)"
            )

    hashes = {relative: _sha256(content) for relative, content in desired.items()}
    manifest_bytes = (json.dumps(
        {"version": _MANIFEST_VERSION, "adapter": "databricks", "files": hashes},
        indent=2, sort_keys=True,
    ) + "\n").encode()
    if manifest is not None and previous == hashes:
        _verify_workspace_publication(workspace, target, desired, manifest_bytes)
        return _result(
            target, "databricks", "unchanged", hashes, compatible_unmanaged,
            legacy_managed,
        )

    before = {**existing, _MANIFEST: manifest_content}
    mutation_order = [*desired, *sorted(set(previous) - set(desired)), _MANIFEST]
    mutated: list[str] = []
    try:
        for relative, content in desired.items():
            if existing.get(relative) == content:
                continue
            mutated.append(relative)
            _workspace_write(
                workspace, _workspace_api_path(target, relative), content, import_format
            )
        for relative in sorted(set(previous) - set(desired)):
            mutated.append(relative)
            _workspace_delete(workspace, _workspace_api_path(target, relative))
        mutated.append(_MANIFEST)
        _workspace_write(workspace, manifest_path, manifest_bytes, import_format)
        _verify_workspace_publication(workspace, target, desired, manifest_bytes)
    except BaseException as publication_error:
        rollback_errors: list[str] = []
        for relative in reversed(dict.fromkeys(mutated)):
            path = _workspace_api_path(target, relative)
            original = before.get(relative)
            try:
                if original is None:
                    _workspace_delete(workspace, path)
                else:
                    _workspace_write(workspace, path, original, import_format)
            except BaseException:
                rollback_errors.append(relative)
        for relative in dict.fromkeys(mutation_order):
            try:
                if _workspace_read(workspace, _workspace_api_path(target, relative)) != before.get(
                    relative
                ):
                    rollback_errors.append(relative)
            except BaseException:
                rollback_errors.append(relative)
        if rollback_errors:
            failed = ", ".join(sorted(set(rollback_errors)))
            raise HostSetupError(
                "Databricks Workspace host guidance publication and rollback failed for: "
                f"{failed}"
            ) from publication_error
        if isinstance(publication_error, Exception):
            raise HostSetupError(
                "Databricks Workspace host guidance publication failed; original state restored"
            ) from publication_error
        raise

    status = "upgraded" if manifest is not None or legacy_managed else "installed"
    return _result(
        target, "databricks", status, hashes, compatible_unmanaged, legacy_managed,
    )


def setup_host(
    target_root: str | os.PathLike[str], *, adapter: str
) -> dict[str, Any]:
    """Reconcile packaged host guidance without overwriting unowned or edited files.

    ``target_root`` is mandatory and never inferred.  All collisions are validated
    before publication; a manifest records the exact bytes owned by Anchor.
    """
    if adapter not in _ADAPTER_FILES:
        raise ValueError("adapter must be one of: amp, chatgpt, claude, databricks")
    workspace_target = (
        _databricks_target_root(target_root) if adapter == "databricks" else None
    )
    target = workspace_target or _target_root(target_root)
    desired = _desired_files(adapter)
    if workspace_target is not None:
        return _setup_databricks_workspace(target, desired)
    manifest = _load_manifest(target)
    if manifest is not None and manifest["adapter"] != adapter:
        raise HostSetupError(
            f"host guidance manifest adapter mismatch: managed={manifest['adapter']}, requested={adapter}"
        )
    previous: dict[str, str] = {} if manifest is None else manifest["files"]
    compatible_unmanaged: list[str] = []
    existing = {
        relative: (
            _regular_bytes(_destination(target, relative), f"destination {relative}")
            if _destination(target, relative).exists()
            or _destination(target, relative).is_symlink()
            else None
        )
        for relative in desired
    }
    legacy_managed: list[str] = []
    if manifest is None:
        desired, previous, compatible_unmanaged, legacy_managed = _legacy_reconciliation(
            desired, existing
        )
    elif _CUSTOM_INSTRUCTIONS not in previous:
        custom = existing.get(_CUSTOM_INSTRUCTIONS)
        if (
            custom is not None
            and b"# Odibi Anchor operating contract" in custom
            and b"agent_bootstrap.py" in custom
        ):
            desired.pop(_CUSTOM_INSTRUCTIONS)
            compatible_unmanaged.append(_CUSTOM_INSTRUCTIONS)
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
        return _result(
            target, adapter, "unchanged", hashes, compatible_unmanaged, legacy_managed,
        )

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
        _verify_publication(target, desired, manifest_bytes)
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
            _cleanup_staging(staging, adapter)
    status = "upgraded" if manifest is not None or legacy_managed else "installed"
    return _result(target, adapter, status, hashes, compatible_unmanaged, legacy_managed)


def _result(
    target: Path,
    adapter: str,
    status: str,
    hashes: dict[str, str],
    compatible_unmanaged: list[str] | None = None,
    legacy_managed: list[str] | None = None,
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
        "verified_file_count": len(hashes),
        "verified_skill_count": sum(
            path.startswith(".assistant/skills/") and path.endswith("/SKILL.md")
            for path in hashes
        ),
        "managed_files": [
            {"path": relative, "sha256": digest} for relative, digest in sorted(hashes.items())
        ],
        "compatible_unmanaged_files": sorted(compatible_unmanaged or []),
        "reconciled_legacy_managed_files": sorted(legacy_managed or []),
        "next_operation": {
            "operation": "review_host_guidance",
            "path": str(target / _ADAPTER_FILES[adapter]),
        },
    }


__all__ = ["HostSetupError", "setup_host"]
