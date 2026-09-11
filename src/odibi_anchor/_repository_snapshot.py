"""Reproducible, local-only Git repository snapshots for PR evidence."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal, Protocol

_DATABRICKS_EVIDENCE_KIND = "databricks_git_folder"
_DATABRICKS_SCOPE_MAX_FILES = 5_000
_DATABRICKS_SCOPE_MAX_BYTES = 64 * 1024 * 1024
_DATABRICKS_CAPABILITIES = MappingProxyType({
    "host_repository_identity": "available",
    "local_worktree_status": "unavailable",
    "git_changed_paths_and_diff": "unavailable",
    "merge_base_and_history": "unavailable",
    "task_scoped_content_diff": "available",
    "task_scoped_write_tracking": "available",
    "pr_readiness": "unavailable",
})


@dataclass(frozen=True)
class ChangedLineRange:
    """An inclusive changed line interval in the current file."""

    path: str
    kind: Literal["added", "modified"]
    start: int
    end: int


@dataclass(frozen=True)
class RepositorySnapshot:
    """Immutable evidence captured from one local Git worktree."""

    schema_version: int
    target_worktree: str
    branch: str | None
    head_sha: str
    configured_target_ref: str
    target_sha: str | None
    merge_base_sha: str | None
    committed_diff_range: str | None
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    changed_line_ranges: tuple[ChangedLineRange, ...]
    captured_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict)
    local_conflict_result: Literal["clear", "conflict", "unknown"] = "unknown"

    def __post_init__(self) -> None:
        def freeze(value: Any) -> Any:
            if isinstance(value, Mapping):
                return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, (set, frozenset)):
                return frozenset(freeze(item) for item in value)
            return value
        object.__setattr__(self, "provenance", freeze(self.provenance))


@dataclass(frozen=True)
class TaskRepositoryBaseline:
    """Immutable Git identity captured when a source-change task is accepted."""

    target_worktree: str
    branch: str
    configured_target_ref: str
    target_sha: str
    merge_base_sha: str
    task_start_head_sha: str
    captured_at: str
    authority_kind: Literal["clean", "adopted"] = "clean"
    adoption_provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnbornTaskRepositoryBaseline:
    """Immutable task start for a pristine branch with no commit."""

    target_worktree: str
    branch: str
    configured_target_ref: str
    captured_at: str


@dataclass(frozen=True)
class DatabricksGitFolderIdentity:
    """Read-only host identity for one Databricks Git Folder checkout."""

    repository_id: str
    workspace_path: str
    branch: str
    head_sha: str
    remote_url: str
    git_provider: str
    evidence_kind: Literal["databricks_git_folder"] = _DATABRICKS_EVIDENCE_KIND


class DatabricksGitFolderProvider(Protocol):
    """Minimal injected boundary for repeated read-only host attestation."""

    provider_id: str

    def capture_identity(
        self, target_worktree: str | os.PathLike[str],
    ) -> DatabricksGitFolderIdentity:
        """Return the current host repository identity or raise."""


@dataclass(frozen=True)
class ScopedFilePreimage:
    """Exact task-start bytes for one regular file in an authorized scope."""

    path: str
    sha256: str
    size: int
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class DatabricksGitFolderTaskBaseline:
    """Task-local filesystem start anchored to a Databricks Repos identity."""

    target_worktree: str
    repository_scope: tuple[str, ...]
    directory_scopes: tuple[str, ...]
    identity: DatabricksGitFolderIdentity
    preimages: tuple[ScopedFilePreimage, ...]
    captured_at: str
    identity_provider: DatabricksGitFolderProvider = field(repr=False, compare=False)
    evidence_kind: Literal["databricks_git_folder"] = _DATABRICKS_EVIDENCE_KIND


@dataclass(frozen=True)
class TaskChangeScope:
    """Immutable task-local source scope without delivery snapshot identities."""

    target_worktree: str
    branch: str
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    changed_line_ranges: tuple[ChangedLineRange, ...]
    captured_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        def freeze(value: Any) -> Any:
            if isinstance(value, Mapping):
                return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, (set, frozenset)):
                return frozenset(freeze(item) for item in value)
            return value
        object.__setattr__(self, "provenance", freeze(self.provenance))


@dataclass(frozen=True)
class DatabricksTaskChangeScope:
    """Task-start-to-current content scope with no local-Git state fields."""

    target_worktree: str
    identity: DatabricksGitFolderIdentity
    changed_paths: tuple[str, ...]
    changed_line_ranges: tuple[ChangedLineRange, ...]
    captured_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence_kind: Literal["databricks_git_folder"] = _DATABRICKS_EVIDENCE_KIND

    def __post_init__(self) -> None:
        def freeze(value: Any) -> Any:
            if isinstance(value, Mapping):
                return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, (set, frozenset)):
                return frozenset(freeze(item) for item in value)
            return value
        object.__setattr__(self, "provenance", freeze(self.provenance))


def is_databricks_git_folder_baseline(value: Any) -> bool:
    """Return whether value is the weaker, explicitly discriminated task baseline."""
    return isinstance(value, DatabricksGitFolderTaskBaseline)


def databricks_repository_capabilities() -> dict[str, str]:
    """Return the public truthful capability projection for Git Folder tasks."""
    return dict(_DATABRICKS_CAPABILITIES)


def _databricks_block(reason: str) -> RuntimeError:
    return RuntimeError(
        "BLOCKED: Databricks Git Folder source evidence is insufficient: "
        f"{reason}. Known host identity fields are limited to repository ID/path, branch, "
        "HEAD, remote URL, and provider. Local Git cleanliness, staged/unstaged/untracked "
        "state, conflicts, merge-base, history, and PR readiness remain unavailable. "
        "Operations that do not consume task-source evidence may remain available under their "
        "normal task policy."
    )


def _canonical_local_git_available(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root,
            capture_output=True, timeout=10, check=False,
        )
        return (
            result.returncode == 0
            and Path(os.fsdecode(result.stdout).strip()).resolve(strict=True) == root
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


def canonical_local_git_available(target_worktree: str | os.PathLike[str]) -> bool:
    """Return whether the exact target is a canonical local Git worktree."""
    try:
        root = Path(target_worktree).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return _canonical_local_git_available(root)


def _provider_identity(
    provider: DatabricksGitFolderProvider,
    target_worktree: str | os.PathLike[str],
) -> DatabricksGitFolderIdentity:
    capture = getattr(provider, "capture_identity", None)
    if not callable(capture):
        raise TypeError("repository_provider must define capture_identity(target_worktree)")
    try:
        raw = capture(target_worktree)
    except Exception as exc:
        raise _databricks_block("read-only host identity acquisition failed") from exc
    names = (
        "repository_id", "workspace_path", "branch", "head_sha", "remote_url", "git_provider",
    )
    if isinstance(raw, Mapping):
        values = {name: raw.get(name) for name in names}
        evidence_kind = raw.get("evidence_kind")
    else:
        values = {name: getattr(raw, name, None) for name in names}
        evidence_kind = getattr(raw, "evidence_kind", None)
    if evidence_kind != _DATABRICKS_EVIDENCE_KIND or any(
        not isinstance(values[name], str) or not values[name].strip() for name in names
    ):
        raise _databricks_block("the provider returned incomplete or non-Databricks identity")
    normalized = {name: values[name].strip() for name in names}
    normalized["head_sha"] = normalized["head_sha"].lower()
    if re.fullmatch(r"[0-9a-f]{40}", normalized["head_sha"]) is None:
        raise _databricks_block("the provider returned a non-exact HEAD commit SHA")
    if not normalized["workspace_path"].startswith("/"):
        raise _databricks_block("the provider returned a non-absolute workspace path")
    return DatabricksGitFolderIdentity(**normalized)


def _scoped_candidate(root: Path, name: str, *, during_capture: bool) -> Path:
    """Keep Databricks evidence scopes lexical, contained, and symlink-free."""
    candidate = root / name
    label = "authorized scope" if during_capture else "repository_scope"
    current = root
    for part in Path(name).parts:
        current /= part
        if current.is_symlink():
            raise _databricks_block(f"{label} contains a symlink: {name or '.'}")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _databricks_block(f"{label} path escapes the target: {name or '.'}") from exc
    return candidate


def _normalize_repository_scope(
    root: Path, repository_scope: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if isinstance(repository_scope, (str, bytes)) or not isinstance(repository_scope, Sequence):
        raise _databricks_block("repository_scope must be a non-empty sequence of relative paths")
    normalized: list[str] = []
    directories: list[str] = []
    for value in repository_scope:
        if not isinstance(value, str) or not value.strip():
            raise _databricks_block("repository_scope entries must be non-empty relative paths")
        raw = value.strip()
        lexical = PurePosixPath(raw.replace("\\", "/"))
        windows = PureWindowsPath(raw)
        if lexical.is_absolute() or windows.is_absolute() or windows.drive:
            raise _databricks_block(
                "repository_scope requires relative paths "
                "(e.g., ['.'] for repository root, ['src'] for src directory); "
                f"absolute paths are not supported: {value!r}"
            )
        if ".." in lexical.parts:
            raise _databricks_block(f"repository_scope path escapes the target: {value!r}")
        name = lexical.as_posix()
        if name == ".":
            name = ""
        candidate = _scoped_candidate(root, name, during_capture=False)
        if candidate.exists() and candidate.is_dir():
            directories.append(name)
        elif candidate.exists() and not candidate.is_file():
            raise _databricks_block(f"repository_scope contains a special file: {value!r}")
        normalized.append(name)
    if not normalized:
        raise _databricks_block("repository_scope is required for bounded source work")
    # Parent scopes subsume children. Preserve an exact stable contract.
    unique = tuple(sorted(set(normalized)))
    return unique, tuple(sorted(set(directories)))


def _read_scoped_file(root: Path, path: Path) -> ScopedFilePreimage:
    relative = path.relative_to(root).as_posix()
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise _databricks_block(f"authorized scope contains a non-regular file: {relative}")
    content = path.read_bytes()
    after = path.stat()
    if (
        before.st_mtime_ns != after.st_mtime_ns
        or before.st_size != after.st_size
        or before.st_ino != after.st_ino
        or len(content) != after.st_size
    ):
        raise _databricks_block(f"authorized file changed while its preimage was read: {relative}")
    return ScopedFilePreimage(
        relative, hashlib.sha256(content).hexdigest(), len(content), content,
    )


def _capture_scoped_files(
    root: Path,
    repository_scope: tuple[str, ...],
) -> tuple[ScopedFilePreimage, ...]:
    files: dict[str, ScopedFilePreimage] = {}
    total_bytes = 0

    def add(path: Path) -> None:
        nonlocal total_bytes
        name = path.relative_to(root).as_posix()
        item = _read_scoped_file(
            root,
            _scoped_candidate(root, name, during_capture=True),
        )
        if item.path in files:
            return
        if len(files) >= _DATABRICKS_SCOPE_MAX_FILES:
            raise _databricks_block(
                f"repository_scope exceeds {_DATABRICKS_SCOPE_MAX_FILES} files; narrow the scope"
            )
        total_bytes += item.size
        if total_bytes > _DATABRICKS_SCOPE_MAX_BYTES:
            raise _databricks_block(
                f"repository_scope exceeds {_DATABRICKS_SCOPE_MAX_BYTES} bytes; narrow the scope"
            )
        files[item.path] = item

    for name in repository_scope:
        candidate = _scoped_candidate(root, name, during_capture=True)
        if not candidate.exists():
            continue
        if candidate.is_file():
            add(candidate)
            continue
        if not candidate.is_dir():
            raise _databricks_block(f"authorized scope contains a special file: {name or '.'}")
        for dirpath, dirnames, filenames in os.walk(candidate, followlinks=False):
            current = Path(dirpath)
            for dirname in tuple(dirnames):
                child = current / dirname
                if child.is_symlink():
                    raise _databricks_block(
                        f"authorized scope contains a symlink: {child.relative_to(root).as_posix()}"
                    )
            for filename in filenames:
                add(current / filename)
    return tuple(files[name] for name in sorted(files))


def capture_databricks_git_folder_task_baseline(
    target_worktree: str | os.PathLike[str],
    provider: DatabricksGitFolderProvider,
    repository_scope: Sequence[str] | None,
    *,
    accept_unknown_git_state: bool = False,
) -> DatabricksGitFolderTaskBaseline:
    """Capture an explicit weaker source baseline without inventing local-Git facts."""
    root = Path(target_worktree).resolve(strict=True)
    if _canonical_local_git_available(root):
        raise _databricks_block(
            "canonical local Git and an explicit Databricks provider are both available; "
            "select exactly one repository authority"
        )
    if accept_unknown_git_state is not True:
        raise _databricks_block(
            "accept_unknown_git_state=True is required because pre-existing Git dirtiness is unknowable"
        )
    scope, directories = _normalize_repository_scope(root, repository_scope or ())
    before = _provider_identity(provider, root)
    preimages = _capture_scoped_files(root, scope)
    after = _provider_identity(provider, root)
    if after != before:
        raise _databricks_block("host repository branch, HEAD, path, remote, or provider moved during capture")
    return DatabricksGitFolderTaskBaseline(
        str(root), scope, directories, before, preimages,
        datetime.now(timezone.utc).isoformat(), provider,
    )


def databricks_task_baseline_projection(
    baseline: DatabricksGitFolderTaskBaseline,
) -> dict[str, Any]:
    """Project safe metadata only; exact preimage bytes remain process-local."""
    return {
        "evidence_kind": baseline.evidence_kind,
        "target_worktree": baseline.target_worktree,
        "repository_scope": list(baseline.repository_scope),
        "identity": {
            "repository_id": baseline.identity.repository_id,
            "workspace_path": baseline.identity.workspace_path,
            "branch": baseline.identity.branch,
            "head_sha": baseline.identity.head_sha,
            "remote_url": baseline.identity.remote_url,
            "git_provider": baseline.identity.git_provider,
        },
        "preimages": [
            {"path": item.path, "sha256": item.sha256, "size": item.size}
            for item in baseline.preimages
        ],
        "capabilities": databricks_repository_capabilities(),
        "captured_at": baseline.captured_at,
    }


def capture_task_repository_baseline(
    target_worktree: str | os.PathLike[str], configured_target_ref: str = "main",
) -> TaskRepositoryBaseline | UnbornTaskRepositoryBaseline:
    """Capture a clean, branch-attached born or exact-unborn task start."""
    root, git = _task_git(target_worktree, configured_target_ref)
    if _optional_commit(git, "HEAD") is None:
        branch = _optional_branch(git)
        if branch is None:
            raise RuntimeError("source-change task requires an attached Git branch")
        if branch != configured_target_ref:
            raise RuntimeError(
                "unborn source-change task requires its current branch to equal the configured target"
            )
        local_target_ref = f"refs/heads/{configured_target_ref}"
        if (_optional_commit(git, local_target_ref) is not None or
                _optional_commit(git, configured_target_ref) is not None):
            raise RuntimeError("unborn source-change task requires an absent configured target")
        staged, unstaged, untracked = _mutable_paths(git)
        if staged or unstaged or untracked:
            raise RuntimeError("BLOCKED: source-change task requires a clean initial Git worktree")
        return UnbornTaskRepositoryBaseline(
            str(root), branch, configured_target_ref,
            datetime.now(timezone.utc).isoformat(),
        )

    snapshot = capture_repository_snapshot(target_worktree, configured_target_ref)
    # Feature branches may already contain committed work.  Task-start cleanliness
    # concerns only mutable worktree/index state; those commits become the immutable
    # task base and are therefore excluded from the task delta.
    if snapshot.staged_paths or snapshot.unstaged_paths or snapshot.untracked_paths:
        raise RuntimeError("BLOCKED: source-change task requires a clean initial Git worktree")
    if snapshot.branch is None:
        raise RuntimeError("source-change task requires an attached Git branch")
    if not snapshot.target_sha or not snapshot.merge_base_sha:
        raise RuntimeError("source-change task requires a resolvable configured target")
    return TaskRepositoryBaseline(
        snapshot.target_worktree, snapshot.branch, configured_target_ref,
        snapshot.target_sha, snapshot.merge_base_sha, snapshot.head_sha,
        snapshot.captured_at,
    )


def _scoped_file_map(
    values: tuple[ScopedFilePreimage, ...],
) -> dict[str, ScopedFilePreimage]:
    return {item.path: item for item in values}


def _scoped_fingerprint(item: ScopedFilePreimage | None) -> str:
    return "absent" if item is None else f"file:{item.sha256}:{item.size}"


def _current_databricks_scope(
    baseline: DatabricksGitFolderTaskBaseline,
) -> tuple[DatabricksGitFolderIdentity, tuple[ScopedFilePreimage, ...]]:
    root = Path(baseline.target_worktree)
    before = _provider_identity(baseline.identity_provider, root)
    current = _capture_scoped_files(root, baseline.repository_scope)
    after = _provider_identity(baseline.identity_provider, root)
    if before != after or after != baseline.identity:
        raise _databricks_block(
            "host repository branch, HEAD, path, remote, or provider moved after task acceptance"
        )
    return after, current


def _databricks_changed_paths(
    baseline: DatabricksGitFolderTaskBaseline,
    current: tuple[ScopedFilePreimage, ...],
) -> tuple[str, ...]:
    started = _scoped_file_map(baseline.preimages)
    now = _scoped_file_map(current)
    return tuple(sorted(
        path for path in set(started) | set(now)
        if _scoped_fingerprint(started.get(path)) != _scoped_fingerprint(now.get(path))
    ))


def _normalize_acknowledged_path(
    baseline: DatabricksGitFolderTaskBaseline, value: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _databricks_block("a source-write path must be a non-empty string")
    root = Path(baseline.target_worktree)
    candidate = Path(value.replace("\\", "/"))
    if ".." in candidate.parts:
        raise _databricks_block(f"source-write path escapes repository_scope: {value!r}")
    if candidate.is_absolute():
        try:
            name = candidate.absolute().relative_to(root.absolute()).as_posix()
        except ValueError as exc:
            raise _databricks_block(f"source-write path is outside repository_scope: {value!r}") from exc
    else:
        name = candidate.as_posix()
    authorized = name in baseline.repository_scope or any(
        directory == "" or name.startswith(f"{directory}/")
        for directory in baseline.directory_scopes
    )
    if not authorized:
        raise _databricks_block(f"source-write path is outside repository_scope: {value!r}")
    return name


def _validate_databricks_acknowledgements(
    baseline: DatabricksGitFolderTaskBaseline,
    current: tuple[ScopedFilePreimage, ...],
    acknowledged_fingerprints: Mapping[str, str] | None,
    *,
    allow_paths: tuple[str, ...] = (),
    incremental_acknowledgement: bool = False,
) -> None:
    acknowledgements = dict(acknowledged_fingerprints or {})
    if any(not isinstance(path, str) or not isinstance(value, str)
           for path, value in acknowledgements.items()):
        raise _databricks_block("task write acknowledgements are malformed")
    now = _scoped_file_map(current)
    allowed = set(allow_paths)
    for path, expected in acknowledgements.items():
        if path in allowed:
            continue
        if _scoped_fingerprint(now.get(path)) != expected:
            raise _databricks_block(
                f"acknowledged path changed concurrently after its last Anchor acknowledgement: {path}"
            )
    changed = set(_databricks_changed_paths(baseline, current))
    unacknowledged = changed - set(acknowledgements) - allowed
    if unacknowledged:
        if incremental_acknowledgement:
            return
        displayed = sorted(unacknowledged)[:8]
        remaining = len(unacknowledged) - len(displayed)
        python_paths = [path for path in displayed if path.endswith(".py")]
        if python_paths:
            recovery = (
                f"; first run anchor('known_bad', changed_files={python_paths!r}) for Python paths; "
                "then acknowledge each intended edit one at a time with "
                "anchor('touched', '<path>')"
            )
        else:
            recovery = "; acknowledge each intended edit with anchor('touched', '<path>')"
        if remaining:
            recovery += (
                f"; {remaining} additional path(s) omitted—handle the displayed paths and retry "
                "to reveal the next bounded set"
            )
        raise _databricks_block(
            "unacknowledged scoped filesystem drift was detected: "
            + ", ".join(displayed)
            + recovery
        )


def verify_databricks_task_preconditions(
    baseline: DatabricksGitFolderTaskBaseline,
    acknowledged_fingerprints: Mapping[str, str] | None,
    *,
    allow_paths: Sequence[str] = (),
    incremental_acknowledgement: bool = False,
) -> None:
    """Re-attest identity and reject changed write preconditions before an effect."""
    normalized = tuple(
        _normalize_acknowledged_path(baseline, value) for value in allow_paths
    )
    if incremental_acknowledgement and len(normalized) != 1:
        raise _databricks_block(
            "incremental acknowledgement requires exactly one named in-scope path"
        )
    _, current = _current_databricks_scope(baseline)
    _validate_databricks_acknowledgements(
        baseline,
        current,
        acknowledged_fingerprints,
        allow_paths=normalized,
        incremental_acknowledgement=incremental_acknowledgement,
    )


def acknowledge_databricks_task_writes(
    baseline: DatabricksGitFolderTaskBaseline,
    acknowledged_fingerprints: Mapping[str, str] | None,
    paths: Sequence[str],
    *,
    incremental: bool = False,
) -> dict[str, str]:
    """Acknowledge only named in-scope writes after a successful source action."""
    normalized = tuple(_normalize_acknowledged_path(baseline, value) for value in paths)
    if incremental and len(normalized) != 1:
        raise _databricks_block(
            "incremental acknowledgement requires exactly one named in-scope path"
        )
    _, current = _current_databricks_scope(baseline)
    _validate_databricks_acknowledgements(
        baseline,
        current,
        acknowledged_fingerprints,
        allow_paths=normalized,
        incremental_acknowledgement=incremental,
    )
    now = _scoped_file_map(current)
    changed = set(_databricks_changed_paths(baseline, current))
    return {
        path: _scoped_fingerprint(now.get(path))
        for path in sorted(changed)
        if path in set(acknowledged_fingerprints or {}) or path in set(normalized)
    }


def _databricks_changed_ranges(
    baseline: DatabricksGitFolderTaskBaseline,
    current: tuple[ScopedFilePreimage, ...],
    changed_paths: tuple[str, ...],
) -> tuple[ChangedLineRange, ...]:
    started = _scoped_file_map(baseline.preimages)
    now = _scoped_file_map(current)
    ranges: list[ChangedLineRange] = []
    for path in changed_paths:
        old_item = started.get(path)
        new_item = now.get(path)
        if new_item is None or b"\x00" in new_item.content[:8192]:
            continue
        try:
            old_lines = old_item.content.decode("utf-8").splitlines() if old_item else []
            new_lines = new_item.content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        diff = "\n".join(difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", n=0,
        ))
        ranges.extend(_ranges_from_diff(diff, path))
    return tuple(ranges)


def _capture_databricks_task_change_scope(
    baseline: DatabricksGitFolderTaskBaseline,
    acknowledged_fingerprints: Mapping[str, str] | None,
) -> DatabricksTaskChangeScope:
    identity, current = _current_databricks_scope(baseline)
    _validate_databricks_acknowledgements(
        baseline, current, acknowledged_fingerprints,
    )
    changed = _databricks_changed_paths(baseline, current)
    started = _scoped_file_map(baseline.preimages)
    now = _scoped_file_map(current)
    created = tuple(path for path in changed if path not in started)
    deleted = tuple(path for path in changed if path not in now)
    content_changes = {
        path: {
            "status": (
                "created" if path in created
                else "deleted" if path in deleted
                else "modified"
            ),
            "start_sha256": started[path].sha256 if path in started else None,
            "final_sha256": now[path].sha256 if path in now else None,
            "start_size": started[path].size if path in started else None,
            "final_size": now[path].size if path in now else None,
        }
        for path in changed
    }
    captured_at = datetime.now(timezone.utc).isoformat()
    return DatabricksTaskChangeScope(
        baseline.target_worktree,
        identity,
        changed,
        _databricks_changed_ranges(baseline, current, changed),
        captured_at,
        {
            "scope_source": _DATABRICKS_EVIDENCE_KIND,
            "capabilities": databricks_repository_capabilities(),
            "repository_scope": baseline.repository_scope,
            "created_paths": created,
            "deleted_paths": deleted,
            "content_changes": content_changes,
            "host_head_start": baseline.identity.head_sha,
            "host_head_current": identity.head_sha,
            "host_identity_stability": "verified",
            "working_tree_status": "unavailable",
            "git_diff": "unavailable",
            "merge_base_and_history": "unavailable",
            "pr_readiness": "unavailable",
            "captured_at": captured_at,
        },
    )


def capture_task_change_scope(
    baseline: TaskRepositoryBaseline | UnbornTaskRepositoryBaseline | DatabricksGitFolderTaskBaseline,
    acknowledged_fingerprints: Mapping[str, str] | None = None,
) -> RepositorySnapshot | TaskChangeScope | DatabricksTaskChangeScope:
    """Capture current task scope and reject branch/history divergence."""
    if isinstance(baseline, DatabricksGitFolderTaskBaseline):
        return _capture_databricks_task_change_scope(baseline, acknowledged_fingerprints)
    if isinstance(baseline, UnbornTaskRepositoryBaseline):
        return _capture_unborn_task_change_scope(baseline)
    if not isinstance(baseline, TaskRepositoryBaseline):
        raise TypeError("unknown task repository baseline kind")

    snapshot = capture_repository_snapshot(
        baseline.target_worktree, baseline.configured_target_ref,
    )
    if snapshot.target_sha is None:
        raise RuntimeError("task configured target disappeared")
    if snapshot.branch != baseline.branch:
        raise RuntimeError("task repository baseline diverged: branch switched or detached")
    git = _Git(Path(baseline.target_worktree))
    git.run(
        "merge-base", "--is-ancestor", baseline.task_start_head_sha, snapshot.head_sha,
        check=False,
    )
    if git.commands[-1]["exit_code"] != 0:
        raise RuntimeError("task repository baseline diverged: HEAD is not a descendant of task start")
    target_drift = snapshot.target_sha != baseline.target_sha
    if target_drift:
        git.run("merge-base", "--is-ancestor", snapshot.target_sha, snapshot.head_sha,
                check=False)
        if git.commands[-1]["exit_code"] != 0:
            raise RuntimeError(
                "task configured target moved and its current SHA is not integrated into HEAD"
            )

    # Re-capture using the immutable task-start commit as the diff base. The target
    # identity remains task-start evidence and drift is reported, never adopted.
    current = capture_repository_snapshot(
        baseline.target_worktree, baseline.task_start_head_sha,
    )
    provenance = dict(current.provenance)
    provenance.update({
        "scope_source": "task_repository_baseline",
        "task_start_head_sha": baseline.task_start_head_sha,
        "target_start_sha": baseline.target_sha,
        "target_current_sha": snapshot.target_sha,
        "target_drift": target_drift,
        "created_paths": tuple(sorted(set(current.untracked_paths) | set(
            _diff_paths(git, baseline.task_start_head_sha, diff_filter="A")
        ))),
    })
    if baseline.authority_kind == "adopted":
        provenance.update({
            "baseline_authority": "adopted",
            "adoption": dict(baseline.adoption_provenance),
        })
    return RepositorySnapshot(
        current.schema_version, current.target_worktree, current.branch,
        current.head_sha, baseline.configured_target_ref, baseline.target_sha,
        baseline.task_start_head_sha, current.committed_diff_range,
        current.staged_paths, current.unstaged_paths, current.untracked_paths,
        current.changed_paths, current.changed_line_ranges, current.captured_at,
        provenance, current.local_conflict_result,
    )


def task_scope_review_diff(
    snapshot: RepositorySnapshot | TaskChangeScope | DatabricksTaskChangeScope,
) -> dict[str, Any]:
    """Project one discriminated task scope into review_context's compact shape."""
    created_paths = set(snapshot.provenance.get("created_paths", ()))
    deleted_paths = set(snapshot.provenance.get("deleted_paths", ()))
    content_changes = snapshot.provenance.get("content_changes", {})
    per_file: dict[str, dict[str, Any]] = {}
    for path in snapshot.changed_paths:
        additions = sum(
            item.end - item.start + 1 for item in snapshot.changed_line_ranges
            if item.path == path
        )
        per_file[path] = {
            "status": (
                "created" if path in created_paths
                else "deleted" if path in deleted_paths
                else "modified"
            ),
            "additions": additions, "deletions": 0,
        }
        if path in content_changes:
            per_file[path].update(dict(content_changes[path]))
    additions = sum(item["additions"] for item in per_file.values())
    if isinstance(snapshot, DatabricksTaskChangeScope):
        metrics = {
            "files_changed": len(per_file), "total_additions": additions,
            "total_deletions": 0, "net_lines": additions,
            "scope_source": _DATABRICKS_EVIDENCE_KIND,
            "host_head_start": snapshot.provenance["host_head_start"],
            "host_head_current": snapshot.provenance["host_head_current"],
            "host_identity_stability": snapshot.provenance["host_identity_stability"],
            "repository_capabilities": dict(snapshot.provenance["capabilities"]),
        }
        return {"metrics": metrics, "samples": {"per_file": per_file}}
    metrics = {
            "files_changed": len(per_file), "total_additions": additions,
            "total_deletions": 0, "net_lines": additions,
            "scope_source": "task_repository_baseline",
            "target_drift": bool(snapshot.provenance.get("target_drift")),
            "target_start_sha": snapshot.provenance.get("target_start_sha"),
            "target_current_sha": snapshot.provenance.get("target_current_sha"),
    }
    samples = {"per_file": per_file}
    if snapshot.provenance.get("baseline_authority") == "adopted":
        metrics["baseline_authority"] = "adopted"
        samples["adoption"] = dict(snapshot.provenance.get("adoption") or {})
    return {"metrics": metrics, "samples": samples}


def _inside(root: Path, candidate: Path) -> Path:
    """Resolve a path and reject traversal, symlink, and reparse escapes."""
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes target worktree: {candidate}") from exc
    return resolved


def _normalize_path(root: Path, value: str) -> str:
    # Preserve Git's lexical identity. Resolution is solely a containment check.
    lexical = Path(value.replace("\\", "/"))
    if lexical.is_absolute() or ".." in lexical.parts:
        raise ValueError(f"invalid Git path: {value!r}")
    # Lexical validation is intentional: resolving the leaf would reject a
    # repository-owned symlink whose recorded target is outside the worktree.
    (root / lexical).absolute().relative_to(root.absolute())
    return lexical.as_posix()


class _Git:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[dict[str, Any]] = []

    def run_bytes(self, *args: str, check: bool = True) -> bytes:
        started = datetime.now(timezone.utc).isoformat()
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, stdin=subprocess.DEVNULL,
                timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.commands.append({"command": ["git", *args], "exit_code": None,
                                  "observed_at": started, "error": type(exc).__name__})
            raise RuntimeError(f"local git command failed: git {' '.join(args)}") from exc
        self.commands.append({"command": ["git", *args], "exit_code": result.returncode,
                              "observed_at": started})
        if check and result.returncode:
            raise RuntimeError(f"local git command failed ({result.returncode}): git {' '.join(args)}")
        return result.stdout

    def run(self, *args: str, check: bool = True) -> str:
        return os.fsdecode(self.run_bytes(*args, check=check))


def _task_git(
    target_worktree: str | os.PathLike[str], configured_target_ref: str,
) -> tuple[Path, _Git]:
    """Validate the Git root and target ref used by a task-local capture."""
    root = Path(target_worktree).resolve(strict=True)
    if not configured_target_ref or configured_target_ref.startswith("-") or any(
            char.isspace() or char in "~^:?*[\\" for char in configured_target_ref):
        raise ValueError("unsafe configured target ref")
    git = _Git(root)
    try:
        top = Path(git.run("rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        if (root / ".git").exists():
            raise
        raise RuntimeError(
            "BLOCKED: source-change task requires a canonical local Git worktree or an "
            "explicit repository provider with task-scoped evidence. The target path is known, "
            "but branch, HEAD, working-tree status, changed paths, merge-base, and history are "
            "unavailable. Read-only orientation/analysis and approved managed-artifact operations "
            "remain allowed."
        ) from exc
    if top != root:
        raise ValueError("target_worktree must be the canonical Git worktree root")
    return root, git


def _optional_commit(git: _Git, ref: str) -> str | None:
    """Resolve one commit, distinguishing only Git's absent-revision status."""
    args = ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    value = git.run(*args, check=False).strip()
    exit_code = git.commands[-1]["exit_code"]
    if exit_code == 0 and value:
        return value
    if exit_code == 1 and not value:
        return None
    raise RuntimeError(f"local git command failed ({exit_code}): git {' '.join(args)}")


def _optional_branch(git: _Git) -> str | None:
    """Return the attached short branch, distinguishing a detached HEAD."""
    args = ("symbolic-ref", "--quiet", "--short", "HEAD")
    value = git.run(*args, check=False).strip()
    exit_code = git.commands[-1]["exit_code"]
    if exit_code == 0 and value:
        return value
    if exit_code == 1 and not value:
        return None
    raise RuntimeError(f"local git command failed ({exit_code}): git {' '.join(args)}")


def _paths(output: bytes, root: Path) -> tuple[str, ...]:
    values = [_normalize_path(root, os.fsdecode(item)) for item in output.split(b"\0") if item]
    return tuple(sorted(set(values)))


def _diff_paths(
    git: _Git,
    *args: str,
    diff_filter: str = "ACDMRTUXB",
) -> tuple[str, ...]:
    """Return all identities, including both sides of deterministic renames."""
    raw = git.run_bytes("diff", "--name-status", "-z", "--find-renames=50%",
                        f"--diff-filter={diff_filter}", *args).split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(raw) and raw[index]:
        status_code = raw[index]
        index += 1
        count = 2 if status_code[:1] in {b"R", b"C"} else 1
        if index + count > len(raw):
            raise RuntimeError("malformed NUL-delimited git name-status output")
        paths.extend(_normalize_path(git.root, os.fsdecode(item)) for item in raw[index:index + count])
        index += count
    return tuple(sorted(set(paths)))


_HUNK = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<oldn>\d+))? \+(?P<new>\d+)(?:,(?P<newn>\d+))? @@")


def _ranges_from_diff(output: str, path: str) -> list[ChangedLineRange]:
    ranges: list[ChangedLineRange] = []
    for line in output.splitlines():
        match = _HUNK.match(line)
        if match:
            count = int(match.group("newn") or "1")
            old_count = int(match.group("oldn") or "1")
            if count:
                start = int(match.group("new"))
                ranges.append(ChangedLineRange(path, "added" if old_count == 0 else "modified",
                                               start, start + count - 1))
    return ranges


def _mutable_paths(git: _Git) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Capture normalized staged, unstaged, and untracked path identities."""
    staged = _diff_paths(git, "--cached")
    unstaged = _diff_paths(git)
    untracked = _paths(
        git.run_bytes("ls-files", "--others", "--exclude-standard", "-z"),
        git.root,
    )
    return staged, unstaged, untracked


def _index_fingerprint(git: _Git) -> str:
    """Hash the staged index projection without creating an unborn index file."""
    staged_entries = git.run_bytes("ls-files", "--stage", "-z")
    ita_aware_diff = git.run_bytes(
        "diff", "--cached", "--raw", "--no-renames", "--no-abbrev",
        "--ita-invisible-in-index", "--root", "-z",
    )
    digest = hashlib.sha256()
    for projection in (staged_entries, ita_aware_diff):
        digest.update(len(projection).to_bytes(8, "big"))
        digest.update(projection)
    return digest.hexdigest()


def _full_current_file_range(root: Path, name: str) -> ChangedLineRange | None:
    """Return one full added range for a present regular UTF-8 text file."""
    path = root / name
    try:
        regular = stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        regular = False
    data = path.read_bytes() if regular else b""
    if not data or b"\x00" in data[:8192]:
        return None
    try:
        count = len(data.decode("utf-8").splitlines())
    except UnicodeDecodeError:
        return None
    if not count:
        return None
    return ChangedLineRange(name, "added", 1, count)


def _capture_unborn_task_change_scope(
    baseline: UnbornTaskRepositoryBaseline,
) -> TaskChangeScope:
    """Capture conceptual-empty task scope without commit ancestry claims."""
    root, git = _task_git(baseline.target_worktree, baseline.configured_target_ref)
    version = git.run("--version").strip()
    if _optional_branch(git) != baseline.branch:
        raise RuntimeError("task repository baseline diverged: branch switched or detached")

    target_current = _optional_commit(git, f"refs/heads/{baseline.configured_target_ref}")
    staged, unstaged, untracked = _mutable_paths(git)
    tracked = _paths(git.run_bytes("ls-files", "-z"), root)
    changed = tuple(sorted(set(tracked) | set(staged) | set(unstaged) | set(untracked)))
    ranges = tuple(
        item for item in (_full_current_file_range(root, name) for name in changed)
        if item is not None
    )
    captured = datetime.now(timezone.utc).isoformat()
    index_fingerprint = _index_fingerprint(git)
    provenance = {
        "scope": "local_only",
        "remote_validated": False,
        "git_version": version,
        "commands": tuple(git.commands),
        "captured_at": captured,
        "scope_source": "task_repository_baseline",
        "task_start_state": "unborn",
        "task_start_head_sha": None,
        "target_start_sha": None,
        "target_current_sha": target_current,
        "target_birth": target_current is not None,
        "target_drift": False,
        "ancestry_basis": "conceptual_empty_start",
        "created_paths": changed,
        "index_fingerprint": index_fingerprint,
        "worktree_fingerprint": _source_state_fingerprint(root, changed),
    }
    return TaskChangeScope(
        str(root), baseline.branch, staged, unstaged, untracked, changed,
        ranges, captured, provenance,
    )


def _fingerprint(root: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    def frame(tag: bytes, value: bytes = b"") -> None:
        digest.update(len(tag).to_bytes(4, "big") + tag)
        digest.update(len(value).to_bytes(8, "big") + value)
    for name in paths:
        path = root / name
        encoded = name.encode("utf-8", "surrogateescape")
        frame(b"path", encoded)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            frame(b"type", b"missing")
            continue
        file_type = stat.S_IFMT(metadata.st_mode)
        frame(b"type", file_type.to_bytes(8, "big"))
        frame(b"mode", (metadata.st_mode & 0o7777).to_bytes(4, "big"))
        if path.is_symlink():
            frame(b"symlink", os.fsencode(os.readlink(path)))
        elif path.is_file():
            frame(b"regular", path.read_bytes())
        elif path.is_dir():
            frame(b"directory")
        else:
            frame(b"special")
    return digest.hexdigest()


def _source_state_fingerprint(root: Path, paths: tuple[str, ...]) -> str:
    """Hash both the exact changed path set and current bytes."""
    digest = hashlib.sha256("\0".join(paths).encode())
    digest.update(_fingerprint(root, paths).encode())
    return digest.hexdigest()


def _managed_paths(root: Path, exclusions: tuple[str, ...]) -> tuple[str, ...]:
    """Enumerate every managed entry exactly, including dangling links and roots."""
    result: set[str] = set()
    for name in exclusions:
        managed = root / name
        if managed.exists() or managed.is_symlink():
            result.add(name)
        if managed.is_dir() and not managed.is_symlink():
            for directory, names, files in os.walk(managed, followlinks=False):
                parent = Path(directory)
                for child in (*names, *files):
                    result.add((parent / child).relative_to(root).as_posix())
    return tuple(sorted(result))


def managed_repository_fingerprint(snapshot: RepositorySnapshot) -> str:
    """Return the current managed-tree fingerprint for explicit draft authorization."""
    exclusions = tuple(item["path"] for item in snapshot.provenance.get("managed_exclusions", ()))
    return _fingerprint(Path(snapshot.target_worktree), _managed_paths(Path(snapshot.target_worktree), exclusions))


def capture_repository_snapshot(
    target_worktree: str | os.PathLike[str], configured_target_ref: str = "main", *,
    artifact_root: str | os.PathLike[str] | None = None,
    caller_attestation: Mapping[str, Any] | None = None,
) -> RepositorySnapshot:
    """Capture local Git state without fetching or consulting a remote service.

    Managed exclusions are internally derived only for a colocated canonical artifact
    root. Tracked files remain source regardless of their name.
    """
    root = Path(target_worktree).resolve(strict=True)
    if not configured_target_ref or configured_target_ref.startswith("-") or any(
            char.isspace() or char in "~^:?*[\\" for char in configured_target_ref):
        raise ValueError("unsafe configured target ref")
    git = _Git(root)
    top = Path(git.run("rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    if top != root:
        raise ValueError("target_worktree must be the canonical Git worktree root")
    version = git.run("--version").strip()
    head = git.run("rev-parse", "HEAD").strip()
    branch_value = git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    branch = branch_value or None
    target = git.run("rev-parse", "--verify", f"{configured_target_ref}^{{commit}}", check=False).strip() or None
    base = git.run("merge-base", target, head, check=False).strip() if target else None
    base = base or None
    committed_range = f"{base}..{head}" if base else None
    committed = _diff_paths(git, base, head) if base else ()
    staged = _diff_paths(git, "--cached")
    unstaged = _diff_paths(git)
    untracked = _paths(git.run_bytes("ls-files", "--others", "--exclude-standard", "-z"), root)
    tracked = set(_paths(git.run_bytes("ls-files", "-z"), root))

    classified: set[str] = set()
    if artifact_root is not None and Path(artifact_root).resolve(strict=True) == root:
        from odibi_anchor._dispatcher._project import managed_artifact_root_names
        classified.update(managed_artifact_root_names())
    def excluded(name: str) -> bool:
        return name not in tracked and any(name == item or name.startswith(item + "/") for item in classified)
    untracked = tuple(path for path in untracked if not excluded(path))
    changed = tuple(sorted(set(committed) | set(staged) | set(unstaged) | set(untracked)))

    ranges: list[ChangedLineRange] = []
    if base:
        # One aggregate merge-base-to-current coordinate model.  Supplying each
        # already-known path after ``--`` avoids ever parsing a filename from a
        # quoted patch header.
        for name in changed:
            if name not in untracked:
                ranges += _ranges_from_diff(git.run(
                    "diff", "--unified=0", "--no-ext-diff", base, "--", name), name)
    for name in untracked:
        current_range = _full_current_file_range(root, name)
        if current_range is not None:
            ranges.append(current_range)

    conflict: Literal["clear", "conflict", "unknown"] = "unknown"
    if base and target:
        git.run("merge-tree", "--write-tree", target, head, check=False)
        code = git.commands[-1]["exit_code"]
        conflict = "clear" if code == 0 else "conflict" if code == 1 else "unknown"
    captured = datetime.now(timezone.utc).isoformat()
    index_fingerprint = _index_fingerprint(git)
    provenance = {
        "scope": "local_only", "remote_validated": False, "git_version": version,
        "commands": tuple(git.commands), "captured_at": captured,
        "caller_attestation": dict(caller_attestation or {}),
        "managed_exclusions": tuple({"path": item, "reason": "explicitly_classified"} for item in sorted(classified)),
        "index_fingerprint": index_fingerprint,
        "worktree_fingerprint": _source_state_fingerprint(root, changed),
        "managed_fingerprint": _fingerprint(root, _managed_paths(root, tuple(sorted(classified)))),
    }
    return RepositorySnapshot(1, str(root), branch, head, configured_target_ref, target, base,
                              committed_range, staged, unstaged, untracked, changed,
                              tuple(sorted(set(ranges), key=lambda r: (r.path, r.start, r.end, r.kind))),
                              captured, provenance, conflict)


def validate_repository_snapshot(
    snapshot: RepositorySnapshot, *, authorized_managed_fingerprint: str | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Validate all state; an explicitly authorized managed draft fingerprint may replace the captured one."""
    root = Path(snapshot.target_worktree)
    git = _Git(root)
    stale: list[str] = []
    if git.run("rev-parse", "HEAD").strip() != snapshot.head_sha:
        stale.append("head")
    target = git.run("rev-parse", "--verify", f"{snapshot.configured_target_ref}^{{commit}}", check=False).strip() or None
    if target != snapshot.target_sha:
        stale.append("target")
    index = _index_fingerprint(git)
    if index != snapshot.provenance.get("index_fingerprint"):
        stale.append("index")
    committed = _diff_paths(git, snapshot.merge_base_sha, snapshot.head_sha) if snapshot.merge_base_sha else ()
    current = set(committed) | set(_diff_paths(git, "--cached")) | set(_diff_paths(git))
    current_untracked = _paths(git.run_bytes("ls-files", "--others", "--exclude-standard", "-z"), root)
    tracked = set(_paths(git.run_bytes("ls-files", "-z"), root))
    exclusions = tuple(item["path"] for item in snapshot.provenance.get("managed_exclusions", ()))
    current.update(name for name in current_untracked if name in tracked or not any(
        name == item or name.startswith(item + "/") for item in exclusions
    ))
    if _source_state_fingerprint(root, tuple(sorted(current))) != snapshot.provenance.get("worktree_fingerprint"):
        stale.append("worktree")
    expected_managed = authorized_managed_fingerprint or snapshot.provenance.get("managed_fingerprint")
    if managed_repository_fingerprint(snapshot) != expected_managed:
        stale.append("managed")
    return not stale, tuple(stale)
