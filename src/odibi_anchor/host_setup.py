"""Idempotent installation of packaged guidance into an explicit host root."""
from __future__ import annotations

import functools
import hashlib
import importlib
import json
import os
import shlex
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from odibi_anchor._bootstrap_phases import (
    BootstrapPhaseTimeout,
    databricks_workspace_client,
    elapsed_ms,
    is_timeout,
    phase,
    phase_timeout,
    record_phase,
    slowest,
    workspace_timeout_policy,
)

_T = TypeVar("_T")
_PHASE = "host_guidance"
_MANIFEST = ".odibi-anchor-host-guidance.json"
_MANIFEST_VERSION = 1
# The host binding and reconcile backups live outside the v1 manifest on purpose:
# older Anchor versions only inspect manifest and packaged paths, so they never
# report these names as unmanaged collisions.
_BINDING = ".odibi-anchor-host-binding.json"
_BINDING_VERSION = 1
_BACKUPS = ".odibi-anchor-host-backups"
_BACKUP_RECEIPT = "BACKUP.json"
_RELEASED_TABLE = "_released_guidance_hashes.json"
_RELEASED_TABLE_FORMAT = "odibi-anchor-released-guidance-hashes-v1"
_CLASSIFICATIONS = ("current", "released_version", "unmanaged_edit", "missing")
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
_DATABRICKS_READ_WORKERS = 8
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


@functools.lru_cache(maxsize=1)
def _released_table() -> dict[str, dict[str, list[str]]]:
    """Return path -> digest -> [first, last] released version for packaged guidance."""
    path = Path(__file__).with_name(_RELEASED_TABLE)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostSetupError(f"released guidance hash table is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("format") != _RELEASED_TABLE_FORMAT or not isinstance(
        value.get("files"), dict
    ):
        raise HostSetupError(f"released guidance hash table has an unsupported schema: {path}")
    table = {relative: dict(digests) for relative, digests in value["files"].items()}
    for relative, digests in _LEGACY_MANAGED_HASHES.items():
        for digest in digests:
            table.setdefault(relative, {}).setdefault(digest, ["legacy", "legacy"])
    return table


def _released_versions(relative: str, digest: str) -> list[str] | None:
    """Return the released version span that shipped these exact bytes, if any."""
    key = relative
    if relative.startswith(".claude/skills/"):
        key = ".assistant/skills/" + relative.removeprefix(".claude/skills/")
    span = _released_table().get(key, {}).get(digest)
    return list(span) if span is not None else None


def _classify(
    relative: str, content: bytes | None, desired: dict[str, bytes], expected: str | None,
) -> dict[str, Any]:
    """Classify one managed path against the active package, manifest and released bytes."""
    wanted = desired.get(relative)
    entry: dict[str, Any] = {
        "path": relative,
        "manifest_sha256": expected,
        "package_sha256": None if wanted is None else _sha256(wanted),
        "actual_sha256": None,
        "manifest_match": False,
        "released_versions": None,
    }
    if content is None:
        entry.update(classification="missing", action="install" if wanted is not None else "forget")
        return entry
    actual = _sha256(content)
    released = _released_versions(relative, actual)
    entry.update(actual_sha256=actual, manifest_match=actual == expected, released_versions=released)
    if wanted is not None and content == wanted:
        entry.update(classification="current", action="keep")
    else:
        anchor_bytes = actual == expected or released is not None
        entry.update(
            classification="released_version" if anchor_bytes else "unmanaged_edit",
            action="replace" if wanted is not None else "delete",
        )
    return entry


def _reconcile_command(
    target: Path, adapter: str, *, apply: bool = False, approve: bool = False,
) -> str:
    words = ["anchor", "setup-host", adapter, "--target", target.as_posix(), "--reconcile"]
    if apply:
        words.append("--apply")
    if approve:
        words.append("--approve-replace-edited")
    return shlex.join(words)


def _reconcile_operation(target: Path, adapter: str) -> dict[str, Any]:
    return {
        "operation": "setup_host.reconcile",
        "arguments": {
            "target_root": target.as_posix(), "adapter": adapter, "reconcile": True, "dry_run": True,
        },
        "copy_ready": _reconcile_command(target, adapter),
        "reason": (
            "Inspect the read-only reconcile plan; applying it backs up every replaced file "
            "first, and unmanaged edits additionally require explicit approval."
        ),
        "requires_owner": False,
        "retry_safety": "read_only",
    }


def _drift_error(target: Path, adapter: str, entries: list[dict[str, Any]]) -> HostSetupError:
    """Report every drifted managed file with its classification instead of a flat refusal."""
    from odibi_anchor._recovery import attach_recovery

    first = entries[0]
    if first["manifest_sha256"] is None:
        headline = f"unmanaged destination collision: {first['path']}"
    else:
        headline = (
            f"modified managed file: {first['path']} (expected {first['manifest_sha256']}, "
            f"actual {first['actual_sha256'] or 'missing'})"
        )
    counts = {
        name: sum(entry["classification"] == name for entry in entries) for name in _CLASSIFICATIONS
    }
    summary = ", ".join(f"{name}={count}" for name, count in counts.items() if count)
    listing = "; ".join(f"{entry['path']}: {entry['classification']}" for entry in entries)
    operation = _reconcile_operation(target, adapter)
    return attach_recovery(
        HostSetupError(
            f"{headline}. Host guidance drift (host_guidance_drift): {len(entries)} file(s) differ "
            f"from the managed manifest or active package ({summary}): {listing}. Setup stopped "
            "before publication and changed nothing. Supported next step: inspect the reconcile "
            f"plan with `{operation['copy_ready']}`. Do not hand-edit, copy or delete managed files."
        ),
        error_code="host_guidance_drift",
        context={
            "target_root": target.as_posix(),
            "adapter": adapter,
            "manifest_path": (target / _MANIFEST).as_posix(),
            "classification_counts": counts,
            "files": entries,
        },
        next_operations=[operation],
    )


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _binding_error(message: str, *, code: str, context: dict[str, Any]) -> HostSetupError:
    from odibi_anchor._recovery import attach_recovery

    return attach_recovery(HostSetupError(message), error_code=code, context=context)


def _resolve_binding(
    target: Path, *, adapter: str, portfolio_config: str | os.PathLike[str], host_id: str | None,
) -> dict[str, Any]:
    """Validate that the portfolio names exactly one host whose instruction root is ``target``."""
    from odibi_anchor.portfolio import load_portfolio_document

    raw = os.fspath(portfolio_config)
    if not raw or "\n" in raw or "\r" in raw or raw.startswith("~") or not Path(raw).is_absolute():
        raise ValueError("portfolio_config must be an explicit absolute, single-line path")
    document = load_portfolio_document(raw)
    hosts = document["portfolio"].get("hosts", {})

    def matches(settings: Any) -> bool:
        configured = settings.get("instruction_root") if isinstance(settings, dict) else None
        if not isinstance(configured, str) or settings.get("adapter") != adapter:
            return False
        if target.as_posix().startswith("/Workspace/"):
            return configured == target.as_posix()
        return Path(configured).resolve() == target

    context = {"config_path": document["path"], "target_root": target.as_posix(), "host_id": host_id}
    if host_id is not None:
        if host_id not in hosts or not matches(hosts[host_id]):
            raise _binding_error(
                f"portfolio {document['path']} host {host_id!r} does not declare adapter "
                f"{adapter} with instruction_root {target.as_posix()}; refusing to bind this host "
                "root to it",
                code="host_binding_mismatch", context=context,
            )
        selected = host_id
    else:
        candidates = sorted(name for name, settings in hosts.items() if matches(settings))
        if len(candidates) != 1:
            raise _binding_error(
                f"portfolio {document['path']} has {len(candidates)} {adapter} hosts whose "
                f"instruction_root is {target.as_posix()} ({', '.join(candidates) or 'none'}); "
                "pass the exact host",
                code="host_binding_mismatch", context={**context, "candidates": candidates},
            )
        selected = candidates[0]
    return {
        "config_path": document["path"],
        "host_id": selected,
        "instruction_root": target.as_posix(),
        "version": _BINDING_VERSION,
    }


def read_host_binding(instruction_root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read and strictly validate one local host binding sidecar; ``None`` when absent."""
    root = Path(os.fspath(instruction_root))
    path = root / _BINDING
    if not os.path.lexists(path):
        return None
    content = _regular_bytes(path, f"host binding {_BINDING}")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostSetupError(f"host binding {path} is not valid JSON") from exc
    expected_keys = {"config_path", "host_id", "instruction_root", "version"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value["version"] != _BINDING_VERSION
        or not all(isinstance(value[key], str) and value[key] for key in expected_keys - {"version"})
        or content != _canonical_json(value)
    ):
        raise HostSetupError(f"host binding {path} is malformed or non-canonical")
    config = value["config_path"]
    if config.startswith("~") or "\n" in config or not Path(config).is_absolute():
        raise HostSetupError(f"host binding {path} config_path must be absolute")
    if Path(value["instruction_root"]).resolve() != root.resolve():
        raise HostSetupError(
            f"host binding {path} names instruction root {value['instruction_root']}, not {root}"
        )
    return value


def _write_binding_local(target: Path, binding: dict[str, Any]) -> None:
    destination = _destination(target, _BINDING)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{_BINDING}.", dir=target)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(binding))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


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
        client = databricks_workspace_client()
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


def _timed_out(exc: BaseException) -> bool:
    """Classify an SDK/transport failure, including explicitly wrapped causes, as a timeout.

    ``__context__`` is ignored: a retried-then-failed call may carry an unrelated
    earlier timeout there. Transport wrappers store the timeout as cause or argument.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if is_timeout(current):
            return True
        pending.extend(
            item for item in (current.__cause__, *current.args) if isinstance(item, BaseException)
        )
    return False


def _workspace_timeout(path: str, started: float) -> BootstrapPhaseTimeout:
    policy = workspace_timeout_policy()
    return phase_timeout(
        phase_name=_PHASE, layer="workspace_api", elapsed=elapsed_ms(started),
        limit_seconds=policy["retry_timeout_seconds"], item=path, setting=policy["setting"],
    )


def _workspace_read(workspace: Any, path: str) -> bytes | None:
    started = time.perf_counter()
    try:
        stream = workspace.download(path)
    except Exception as exc:
        if _workspace_missing(exc):
            return None
        if _timed_out(exc):
            raise _workspace_timeout(path, started) from exc
        raise HostSetupError(f"Workspace guidance path is inaccessible: {path}") from exc
    try:
        content = stream.read()
    except Exception as exc:
        if _timed_out(exc):
            raise _workspace_timeout(path, started) from exc
        raise HostSetupError(f"Workspace guidance path is unreadable: {path}") from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    if not isinstance(content, bytes):
        raise HostSetupError(f"Workspace guidance path did not return bytes: {path}")
    return content


def _run_bounded(
    calls: list[tuple[str, Callable[[], _T]]], *, sub_phase: str,
    policy: dict[str, Any] | None = None,
) -> list[_T]:
    """Run Workspace calls concurrently; refuse any call that outlives its deadline.

    Each call's deadline starts when a worker begins it. Results keep input order and
    the first failure in input order is raised, as ``executor.map`` did. A hung worker
    cannot be killed, so a timeout returns without waiting for it.
    """
    resolved = workspace_timeout_policy() if policy is None else policy
    deadline = float(resolved["item_deadline_seconds"])
    started: dict[int, float] = {}

    def run(index: int, call: Callable[[], _T]) -> _T:
        started[index] = time.perf_counter()
        return call()

    executor = ThreadPoolExecutor(
        max_workers=min(_DATABRICKS_READ_WORKERS, len(calls)),
        thread_name_prefix="anchor-guidance",
    )
    timed_out = False
    try:
        futures: dict[Future[_T], int] = {
            executor.submit(run, index, call): index for index, (_item, call) in enumerate(calls)
        }
        pending = set(futures)
        while pending:
            _done, pending = wait(
                pending, timeout=min(0.25, deadline / 4), return_when=FIRST_COMPLETED
            )
            now = time.perf_counter()
            for future in pending:
                index = futures[future]
                begun = started.get(index)
                if begun is not None and now - begun > deadline:
                    timed_out = True
                    raise phase_timeout(
                        phase_name=_PHASE, sub_phase=sub_phase, layer="workspace_api",
                        elapsed=(now - begun) * 1000.0, limit_seconds=deadline,
                        item=calls[index][0], setting=resolved["setting"],
                    )
        return [future.result() for future in futures]
    finally:
        executor.shutdown(wait=not timed_out, cancel_futures=True)


def _workspace_read_many(
    workspace: Any, target: Path, relative_paths: list[str], *,
    policy: dict[str, Any] | None = None,
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, bytes | None]:
    """Read complete Workspace files concurrently while preserving input order."""
    ordered = list(dict.fromkeys(relative_paths))
    if not ordered:
        return {}

    def reader(relative: str) -> Callable[[], bytes | None]:
        def read() -> bytes | None:
            started = time.perf_counter()
            content = _workspace_read(workspace, _workspace_api_path(target, relative))
            if observations is not None:
                observations.append({
                    "path": relative,
                    "elapsed_ms": elapsed_ms(started),
                    "bytes": None if content is None else len(content),
                })
            return content

        return read

    contents = _run_bounded(
        [(_workspace_api_path(target, relative), reader(relative)) for relative in ordered],
        sub_phase="read", policy=policy,
    )
    return dict(zip(ordered, contents, strict=True))


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
    policy = workspace_timeout_policy()
    try:
        workspace_types = importlib.import_module("databricks.sdk.service.workspace")
        workspace = databricks_workspace_client(policy).workspace
        import_format = workspace_types.ImportFormat.AUTO
    except Exception as exc:
        raise HostSetupError("Databricks Workspace host setup requires an authenticated SDK") from exc

    with phase("read", layer="workspace_api") as read_phase:
        target_path = _workspace_api_path(target)
        status_started = time.perf_counter()
        try:
            [target_status] = _run_bounded(
                [(target_path, lambda: workspace.get_status(target_path))],
                sub_phase="read", policy=policy,
            )
        except BootstrapPhaseTimeout:
            raise
        except Exception as exc:
            if _timed_out(exc):
                raise _workspace_timeout(target_path, status_started) from exc
            raise HostSetupError(
                f"Databricks Workspace target is inaccessible: {target_path}"
            ) from exc
        raw_object_type = getattr(target_status, "object_type", None)
        object_type = str(getattr(raw_object_type, "value", raw_object_type)).upper()
        if object_type not in {"DIRECTORY", "REPO"}:
            raise HostSetupError("Databricks Workspace target must be a directory or Git Folder")

        manifest_path = _workspace_api_path(target, _MANIFEST)
        [manifest_content] = _run_bounded(
            [(manifest_path, lambda: _workspace_read(workspace, manifest_path))],
            sub_phase="read", policy=policy,
        )
        manifest = None if manifest_content is None else _parse_manifest(manifest_content)
        if manifest is not None and manifest["adapter"] != "databricks":
            raise HostSetupError(
                "host guidance manifest adapter mismatch: "
                f"managed={manifest['adapter']}, requested=databricks"
            )
        previous: dict[str, str] = {} if manifest is None else manifest["files"]
        compatible_unmanaged: list[str] = []
        observations: list[dict[str, Any]] = []
        existing = _workspace_read_many(
            workspace, target, sorted(set(previous) | set(desired)),
            policy=policy, observations=observations,
        )
        read_phase.update(
            file_count=sum(item["bytes"] is not None for item in observations)
            + (manifest_content is not None),
            bytes=sum(item["bytes"] or 0 for item in observations) + len(manifest_content or b""),
            slowest_files=slowest(observations),
            timeout_policy=policy,
        )
    with phase("verify"):
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
        drift: list[dict[str, Any]] = []
        for relative in sorted(set(previous) | set(desired)):
            content = existing.get(relative)
            expected = previous.get(relative)
            if content is not None:
                actual = _sha256(content)
                if expected is None:
                    if relative in desired and actual == _sha256(desired[relative]):
                        continue
                    drift.append(_classify(relative, content, desired, expected))
                elif actual != expected:
                    drift.append(_classify(relative, content, desired, expected))
            elif expected is not None:
                drift.append(_classify(relative, content, desired, expected))
        if drift:
            raise _drift_error(target, "databricks", drift)

        hashes = {relative: _sha256(content) for relative, content in desired.items()}
        manifest_bytes = _canonical_json(
            {"version": _MANIFEST_VERSION, "adapter": "databricks", "files": hashes}
        )
    if manifest is not None and previous == hashes:
        # The reads above already verified every managed file against the prior
        # manifest. Re-reading the same publication doubles Workspace API traffic
        # without adding drift evidence; post-mutation verification remains below.
        record_phase("publish", outcome="not_required", reason="managed files unchanged")
        return _result(
            target, "databricks", "unchanged", hashes, compatible_unmanaged,
            legacy_managed,
        )

    with phase("publish", layer="workspace_api"):
        _publish_workspace(
            workspace, target, desired=desired, obsolete=sorted(set(previous) - set(desired)),
            existing=existing, manifest_content=manifest_content, manifest_bytes=manifest_bytes,
            import_format=import_format,
        )

    status = "upgraded" if manifest is not None or legacy_managed else "installed"
    return _result(
        target, "databricks", status, hashes, compatible_unmanaged, legacy_managed,
    )


def _publish_workspace(
    workspace: Any, target: Path, *, desired: dict[str, bytes], obsolete: list[str],
    existing: dict[str, bytes | None], manifest_content: bytes | None, manifest_bytes: bytes,
    import_format: Any,
) -> None:
    """Publish desired bytes through the Workspace API, restoring the original on failure."""
    manifest_path = _workspace_api_path(target, _MANIFEST)
    before = {**existing, _MANIFEST: manifest_content}
    mutation_order = [*desired, *obsolete, _MANIFEST]
    mutated: list[str] = []
    try:
        for relative, content in desired.items():
            if existing.get(relative) == content:
                continue
            mutated.append(relative)
            _workspace_write(
                workspace, _workspace_api_path(target, relative), content, import_format
            )
        for relative in obsolete:
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
                if _workspace_read(
                    workspace, _workspace_api_path(target, relative)
                ) != before.get(relative):
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


def _workspace_handles() -> tuple[Any, Any]:
    try:
        workspace_types = importlib.import_module("databricks.sdk.service.workspace")
        workspace = databricks_workspace_client(workspace_timeout_policy()).workspace
        return workspace, workspace_types.ImportFormat.AUTO
    except Exception as exc:
        raise HostSetupError("Databricks Workspace host setup requires an authenticated SDK") from exc


def _workspace_missing_path(workspace: Any, path: str) -> bool:
    try:
        workspace.get_status(path)
    except Exception as exc:
        if _workspace_missing(exc):
            return True
        raise HostSetupError(f"Databricks Workspace path is inaccessible: {path}") from exc
    return False


def _compatible_unmanaged(
    adapter: str, previous: dict[str, str], desired: dict[str, bytes],
    existing: dict[str, bytes | None],
) -> list[str]:
    """Leave customized, user-owned guidance in place, as plain setup does."""
    compatible: list[str] = []
    custom = existing.get(_CUSTOM_INSTRUCTIONS)
    if (
        _CUSTOM_INSTRUCTIONS not in previous
        and custom is not None
        and custom != desired.get(_CUSTOM_INSTRUCTIONS)
        and _released_versions(_CUSTOM_INSTRUCTIONS, _sha256(custom)) is None
        and b"# Odibi Anchor operating contract" in custom
        and b"agent_bootstrap.py" in custom
    ):
        desired.pop(_CUSTOM_INSTRUCTIONS, None)
        compatible.append(_CUSTOM_INSTRUCTIONS)
    host_file = _ADAPTER_FILES[adapter]
    pointer = existing.get(host_file)
    if (
        host_file in _POINTERS
        and host_file not in previous
        and pointer is not None
        and pointer != desired.get(host_file)
        and b".assistant_instructions.md" in pointer
    ):
        desired.pop(host_file, None)
        compatible.append(host_file)
    return compatible


def _approval_error(
    target: Path, adapter: str, edits: list[dict[str, Any]], planned_backup: str,
) -> HostSetupError:
    from odibi_anchor._recovery import attach_recovery

    return attach_recovery(
        HostSetupError(
            "host guidance reconcile refused (host_guidance_edit_approval_required): "
            f"{len(edits)} managed file(s) contain unmanaged edits that match neither the "
            f"active package, the manifest, nor any released Anchor version: "
            f"{', '.join(entry['path'] for entry in edits)}. Nothing was written. Replacing them "
            "requires explicit owner approval; every replaced file is backed up first to "
            f"{planned_backup}."
        ),
        error_code="host_guidance_edit_approval_required",
        context={
            "target_root": target.as_posix(),
            "adapter": adapter,
            "unmanaged_edits": edits,
            "planned_backup_root": planned_backup,
        },
        next_operations=[{
            "operation": "setup_host.reconcile",
            "arguments": {
                "target_root": target.as_posix(), "adapter": adapter, "reconcile": True,
                "dry_run": False, "approve_replace_edited": True,
            },
            "copy_ready": _reconcile_command(target, adapter, apply=True, approve=True),
            "reason": "Replace the edited files after the owner reviews the backed-up edits.",
            "requires_owner": True,
            "retry_safety": "idempotent",
        }],
    )


def _backup_local(target: Path, stamp: str, files: dict[str, bytes]) -> Path:
    """Write every file to an exclusive, verified backup directory before replacement."""
    # _destination refuses a symlinked or non-directory backup parent.
    parent = _destination(target, f"{_BACKUPS}/{stamp}").parent
    parent.mkdir(exist_ok=True)
    backup_root = parent / stamp
    backup_root.mkdir()
    for relative, content in files.items():
        destination = backup_root.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    for relative, content in files.items():
        saved = backup_root.joinpath(*PurePosixPath(relative).parts)
        if _regular_bytes(saved, f"backup {relative}") != content:
            raise HostSetupError(f"host guidance backup verification failed: {relative}")
    return backup_root


def _backup_workspace(
    workspace: Any, target: Path, stamp: str, files: dict[str, bytes], import_format: Any,
) -> Path:
    backup_relative = f"{_BACKUPS}/{stamp}"
    if not _workspace_missing_path(workspace, _workspace_api_path(target, backup_relative)):
        raise HostSetupError(f"host guidance backup already exists: {backup_relative}")
    for relative, content in files.items():
        _workspace_write(
            workspace, _workspace_api_path(target, f"{backup_relative}/{relative}"), content,
            import_format,
        )
    for relative, content in files.items():
        path = _workspace_api_path(target, f"{backup_relative}/{relative}")
        if _workspace_read(workspace, path) != content:
            raise HostSetupError(f"host guidance backup verification failed: {relative}")
    return target / _BACKUPS / stamp


def _reconcile_host(
    target: Path, adapter: str, *, workspace_target: bool, dry_run: bool, approve: bool,
) -> dict[str, Any]:
    """Classify, back up, reinstall and verify every managed file; dry run by default."""
    from odibi_anchor import __version__

    desired = _desired_files(adapter)
    workspace: Any = None
    import_format: Any = None
    if workspace_target:
        workspace, import_format = _workspace_handles()
        target_path = _workspace_api_path(target)
        try:
            status = workspace.get_status(target_path)
        except Exception as exc:
            raise HostSetupError(f"Databricks Workspace target is inaccessible: {target_path}") from exc
        raw_type = getattr(status, "object_type", None)
        if str(getattr(raw_type, "value", raw_type)).upper() not in {"DIRECTORY", "REPO"}:
            raise HostSetupError("Databricks Workspace target must be a directory or Git Folder")
        manifest_content = _workspace_read(workspace, _workspace_api_path(target, _MANIFEST))
    else:
        manifest_file = target / _MANIFEST
        manifest_content = (
            _regular_bytes(manifest_file, f"managed manifest {_MANIFEST}")
            if os.path.lexists(manifest_file)
            else None
        )
    manifest = None if manifest_content is None else _parse_manifest(manifest_content)
    if manifest is not None and manifest["adapter"] != adapter:
        raise HostSetupError(
            f"host guidance manifest adapter mismatch: managed={manifest['adapter']}, requested={adapter}"
        )
    previous: dict[str, str] = {} if manifest is None else manifest["files"]
    paths = sorted(set(previous) | set(desired))
    if workspace_target:
        existing = _workspace_read_many(workspace, target, paths)
    else:
        existing = {}
        for relative in paths:
            destination = _destination(target, relative)
            existing[relative] = (
                _regular_bytes(destination, f"destination {relative}")
                if destination.exists() or destination.is_symlink()
                else None
            )
    compatible = _compatible_unmanaged(adapter, previous, desired, existing)
    entries = [
        _classify(relative, existing.get(relative), desired, previous.get(relative))
        for relative in sorted(set(previous) | set(desired))
    ]
    hashes = {relative: _sha256(content) for relative, content in desired.items()}
    manifest_bytes = _canonical_json(
        {"version": _MANIFEST_VERSION, "adapter": adapter, "files": hashes}
    )
    edits = [entry for entry in entries if entry["classification"] == "unmanaged_edit"]
    replaced = [entry["path"] for entry in entries if entry["action"] in {"replace", "delete"}]
    needs_publication = (
        any(entry["action"] != "keep" for entry in entries) or manifest_content != manifest_bytes
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    planned_backup = (target / _BACKUPS / stamp).as_posix()
    plan: dict[str, Any] = {
        "kind": "host_guidance_reconcile",
        "status": "planned",
        "dry_run": dry_run,
        "adapter": adapter,
        "target_root": target.as_posix(),
        "manifest_path": (target / _MANIFEST).as_posix(),
        "package_version": __version__,
        "classification_counts": {
            name: sum(entry["classification"] == name for entry in entries)
            for name in _CLASSIFICATIONS
        },
        "files": entries,
        "compatible_unmanaged_files": sorted(compatible),
        "approval_required": [entry["path"] for entry in edits],
        "approved": approve,
        "backup": {
            "root": None,
            "planned_root": planned_backup if needs_publication else None,
            "files": replaced + ([_MANIFEST] if needs_publication and manifest_content else []),
        },
        "next_operation": {
            "operation": "review_host_guidance",
            "path": (target / _ADAPTER_FILES[adapter]).as_posix(),
        },
    }
    if not needs_publication:
        plan.update(status="unchanged")
        plan["backup"]["files"] = []
        return plan
    if dry_run:
        plan["next_operation"] = {
            "operation": "setup_host.reconcile",
            "arguments": {
                "target_root": target.as_posix(), "adapter": adapter, "reconcile": True,
                "dry_run": False, "approve_replace_edited": bool(edits),
            },
            "copy_ready": _reconcile_command(target, adapter, apply=True, approve=bool(edits)),
            "reason": (
                "Apply the plan: back up every replaced file, reinstall from the active package, "
                "and verify the manifest and hashes."
            ),
            "requires_owner": bool(edits),
            "retry_safety": "idempotent",
        }
        return plan
    if edits and not approve:
        raise _approval_error(target, adapter, edits, planned_backup)

    backups: dict[str, bytes] = {}
    for relative in replaced:
        content = existing[relative]
        assert content is not None  # replace/delete actions are only planned for present files
        backups[relative] = content
    if manifest_content is not None:
        backups[_MANIFEST] = manifest_content
    receipt = _canonical_json({
        "format": "odibi-anchor-host-guidance-backup-v1",
        "created_at": stamp,
        "adapter": adapter,
        "target_root": target.as_posix(),
        "package_version": __version__,
        "files": [
            {key: entry[key] for key in ("path", "classification", "actual_sha256", "action")}
            for entry in entries
            if entry["path"] in backups
        ],
        "manifest_sha256": None if manifest_content is None else _sha256(manifest_content),
    })
    obsolete = [
        entry["path"] for entry in entries if entry["action"] == "delete"
    ]
    if workspace_target:
        backup_root = _backup_workspace(
            workspace, target, stamp, {**backups, _BACKUP_RECEIPT: receipt}, import_format,
        )
        _publish_workspace(
            workspace, target, desired=desired, obsolete=obsolete, existing=existing,
            manifest_content=manifest_content, manifest_bytes=manifest_bytes,
            import_format=import_format,
        )
        verified_manifest = _workspace_read(workspace, _workspace_api_path(target, _MANIFEST))
    else:
        backup_root = _backup_local(target, stamp, {**backups, _BACKUP_RECEIPT: receipt})
        _publish_local(
            target, adapter, desired=desired, obsolete=obsolete, manifest_bytes=manifest_bytes,
        )
        verified_manifest = _regular_bytes(target / _MANIFEST, f"published {_MANIFEST}")
    if verified_manifest is None or _parse_manifest(verified_manifest)["files"] != hashes:
        raise HostSetupError("published host guidance verification failed: manifest")
    result = _result(target, adapter, "reconciled", hashes, compatible)
    plan.update(
        status="reconciled",
        verified_file_count=result["verified_file_count"],
        verified_skill_count=result["verified_skill_count"],
        managed_files=result["managed_files"],
        next_operation=result["next_operation"],
    )
    plan["backup"].update(root=backup_root.as_posix(), planned_root=None, receipt=_BACKUP_RECEIPT)
    return plan


def _publish_binding(
    target: Path, binding: dict[str, Any], *, workspace_target: bool, dry_run: bool,
) -> dict[str, Any]:
    """Record the exact portfolio and host for deterministic launcher discovery."""
    content = _canonical_json(binding)
    path = (target / _BINDING).as_posix()
    if workspace_target:
        workspace, import_format = _workspace_handles()
        current = _workspace_read(workspace, _workspace_api_path(target, _BINDING))
    else:
        workspace = import_format = None
        current = (
            _regular_bytes(target / _BINDING, f"host binding {_BINDING}")
            if os.path.lexists(target / _BINDING)
            else None
        )
    previous: Any = None
    if current is not None:
        try:
            previous = json.loads(current)
        except (UnicodeDecodeError, json.JSONDecodeError):
            previous = {"status": "invalid", "sha256": _sha256(current)}
    status = "created" if current is None else "unchanged" if current == content else "replaced"
    report = {"path": path, "status": status, "binding": binding, "previous": previous}
    if dry_run or status == "unchanged":
        if dry_run and status != "unchanged":
            report["status"] = f"planned_{status}"
        return report
    if workspace_target:
        _workspace_write(workspace, _workspace_api_path(target, _BINDING), content, import_format)
        written = _workspace_read(workspace, _workspace_api_path(target, _BINDING))
    else:
        _write_binding_local(target, binding)
        written = _regular_bytes(target / _BINDING, f"host binding {_BINDING}")
    if written != content:
        raise HostSetupError(f"host binding verification failed: {path}")
    return report


def setup_host(
    target_root: str | os.PathLike[str],
    *,
    adapter: str,
    portfolio_config: str | os.PathLike[str] | None = None,
    host_id: str | None = None,
    reconcile: bool = False,
    dry_run: bool | None = None,
    approve_replace_edited: bool = False,
) -> dict[str, Any]:
    """Install packaged host guidance, or reconcile drift with ``reconcile=True``.

    Plain setup never overwrites unowned or edited files and raises
    ``host_guidance_drift`` with a per-file classification. ``reconcile=True`` is a
    dry run unless ``dry_run=False``; applying backs up every replaced file and
    replaces unmanaged edits only with ``approve_replace_edited=True``.
    ``portfolio_config`` records the exact portfolio and host in the host binding
    sidecar read by the managed launcher.
    """
    if adapter not in _ADAPTER_FILES:
        raise ValueError("adapter must be one of: amp, chatgpt, claude, databricks")
    effective_dry_run = reconcile if dry_run is None else dry_run
    if not reconcile and (effective_dry_run or approve_replace_edited):
        raise ValueError("dry_run and approve_replace_edited apply only with reconcile=True")
    if host_id is not None and portfolio_config is None:
        raise ValueError("host_id requires portfolio_config")
    workspace_target = (
        _databricks_target_root(target_root) if adapter == "databricks" else None
    )
    target = workspace_target or _target_root(target_root)
    binding = (
        None
        if portfolio_config is None
        else _resolve_binding(
            target, adapter=adapter, portfolio_config=portfolio_config, host_id=host_id
        )
    )
    if reconcile:
        result = _reconcile_host(
            target, adapter, workspace_target=workspace_target is not None,
            dry_run=effective_dry_run, approve=approve_replace_edited,
        )
    else:
        result = _install_host(target_root, adapter=adapter)
    if binding is not None:
        result["host_binding"] = _publish_binding(
            target, binding, workspace_target=workspace_target is not None,
            dry_run=effective_dry_run,
        )
    return result


def _install_host(
    target_root: str | os.PathLike[str], *, adapter: str
) -> dict[str, Any]:
    """Reconcile packaged host guidance without overwriting unowned or edited files.

    ``target_root`` is mandatory and never inferred.  All collisions are validated
    before publication; a manifest records the exact bytes owned by Anchor.
    """
    workspace_target = (
        _databricks_target_root(target_root) if adapter == "databricks" else None
    )
    target = workspace_target or _target_root(target_root)
    with phase("package_resources", layer="filesystem"):
        desired = _desired_files(adapter)
    if workspace_target is not None:
        return _setup_databricks_workspace(target, desired)
    with phase("read", layer="filesystem") as read_phase:
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
        read_phase.update(
            file_count=sum(content is not None for content in existing.values()),
            bytes=sum(len(content) for content in existing.values() if content is not None),
        )
    with phase("verify"):
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

        drift: list[dict[str, Any]] = []
        for relative in sorted(set(previous) | set(desired)):
            destination = _destination(target, relative)
            expected = previous.get(relative)
            if destination.exists() or destination.is_symlink():
                content = _regular_bytes(destination, f"destination {relative}")
                actual = _sha256(content)
                if expected is None:
                    if relative in desired and actual == _sha256(desired[relative]):
                        continue
                    drift.append(_classify(relative, content, desired, expected))
                elif actual != expected:
                    drift.append(_classify(relative, content, desired, expected))
            elif expected is not None:
                drift.append(_classify(relative, None, desired, expected))
        if drift:
            raise _drift_error(target, adapter, drift)

        hashes = {relative: _sha256(content) for relative, content in desired.items()}
        manifest_bytes = _canonical_json(
            {"version": _MANIFEST_VERSION, "adapter": adapter, "files": hashes}
        )
    unchanged = manifest is not None and previous == hashes
    if unchanged:
        record_phase("publish", outcome="not_required", reason="managed files unchanged")
        return _result(
            target, adapter, "unchanged", hashes, compatible_unmanaged, legacy_managed,
        )

    with phase("publish", layer="filesystem"):
        _publish_local(
            target, adapter, desired=desired, obsolete=sorted(set(previous) - set(desired)),
            manifest_bytes=manifest_bytes,
        )
    status = "upgraded" if manifest is not None or legacy_managed else "installed"
    return _result(target, adapter, status, hashes, compatible_unmanaged, legacy_managed)


def _publish_local(
    target: Path, adapter: str, *, desired: dict[str, bytes], obsolete: list[str],
    manifest_bytes: bytes,
) -> None:
    """Stage, swap and verify desired bytes locally, restoring the original on failure."""
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
        for relative in obsolete:
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


__all__ = ["HostSetupError", "read_host_binding", "setup_host"]
