"""_session.py — Session management functions.

Extracted from agent_init.py Phase 2 (revamp spec).
Config management, session file registry, session diff, and cross-session persistence.
"""
import hashlib as _hashlib
import json as _json
import os as _os
import re as _re
import tempfile as _tempfile
from contextlib import contextmanager as _contextmanager
from contextlib import suppress as _suppress
from datetime import UTC as _UTC
from datetime import datetime as _datetime
from pathlib import Path as _Path

from odibi_anchor._utils._session_state import (
    _SESSION_FILES_CHANGED,
    _SESSION_FILES_CREATED,
    _SESSION_TIMINGS,
)
from odibi_anchor._utils._session_state import (
    get_diff as _get_session_diff,
)
from odibi_anchor._utils._session_state import (
    touched as _session_touched,
)
from odibi_anchor._utils.contract import build_base_context

# ─── Cross-session state persistence ─────────────────────────────────────────
_SESSION_STATE_FILE = ".anchor_session_state.json"
_CAN_FSYNC_DIRECTORY = _os.name != "nt"
_CONTINUITY_SCHEMA_VERSION = 1
_SAFE_COMPONENT = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ContinuityUnavailable(RuntimeError):
    """A route-owned continuity record cannot be read or written safely."""


def _canonical_json(value: object) -> str:
    return _json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _component(value: str) -> str:
    if _SAFE_COMPONENT.fullmatch(value):
        return value
    return "sha256-" + _hashlib.sha256(value.encode("utf-8")).hexdigest()


def _continuity_owner(route_binding: object, session_state: object) -> dict:
    return {
        "schema_version": _CONTINUITY_SCHEMA_VERSION,
        "project_id": route_binding.project_id,
        "anchor_home": route_binding.anchor_home,
        "target_root": route_binding.target_root,
        "artifact_root": route_binding.artifact_root,
        "route_schema_version": route_binding.schema_version,
        "route_fingerprint": route_binding.fingerprint(),
        "runtime_instance_id": route_binding.runtime_instance_id,
        "session_id": session_state.session_id,
        "task_window_id": session_state.task_window_id,
    }


def _stable_owner(owner: dict) -> dict:
    return {
        key: owner[key]
        for key in ("schema_version", "project_id", "anchor_home", "target_root", "artifact_root")
    }


def _continuity_paths(route_binding: object, session_state: object) -> tuple[str, str, str]:
    base = _os.path.join(route_binding.artifact_root, "continuity", "v1")
    record = _os.path.join(
        base, _component(route_binding.project_id),
        _component(route_binding.runtime_instance_id),
        _component(session_state.session_id) + ".json",
    )
    return base, _os.path.join(base, "OWNER.json"), record


def _fsync_directory(directory: str) -> None:
    if not _CAN_FSYNC_DIRECTORY:
        return
    descriptor = _os.open(directory, _os.O_RDONLY)
    try:
        _os.fsync(descriptor)
    finally:
        _os.close(descriptor)


def _write_create_only(path: str, payload: bytes) -> bool:
    try:
        descriptor = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with _os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        _os.fsync(stream.fileno())
    _fsync_directory(_os.path.dirname(path))
    return True


def _ensure_owner_sentinel(route_binding: object, session_state: object) -> dict:
    base, sentinel_path, _ = _continuity_paths(route_binding, session_state)
    _os.makedirs(base, exist_ok=True)
    expected = _stable_owner(_continuity_owner(route_binding, session_state))
    encoded = (_canonical_json(expected) + "\n").encode("utf-8")
    _write_create_only(sentinel_path, encoded)
    try:
        with open(sentinel_path, "rb") as stream:
            existing = stream.read()
    except OSError as exc:
        raise ContinuityUnavailable("continuity owner sentinel is unreadable") from exc
    if existing != encoded:
        raise ContinuityUnavailable(
            "continuity owner mismatch; use the artifact root bound to this project route"
        )
    return expected


def _verified_record(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            record = _json.load(stream)
    except (OSError, _json.JSONDecodeError) as exc:
        raise ContinuityUnavailable("continuity record is unreadable or malformed") from exc
    if not isinstance(record, dict) or record.get("format") != "odibi-anchor-continuity-v1":
        raise ContinuityUnavailable("unsupported continuity record format")
    digest = record.pop("record_sha256", None)
    actual = _hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()
    record["record_sha256"] = digest
    if digest != actual:
        raise ContinuityUnavailable("continuity record checksum mismatch")
    return record


def _relocate_restored_continuity(
    staged_projects_root: str | _os.PathLike[str],
    destination_projects_root: str | _os.PathLike[str],
) -> dict:
    """Rebase verified continuity paths in an unpublished restored artifact tree."""
    staged_root = _Path(staged_projects_root)
    destination_root = _Path(destination_projects_root)
    owner_paths = sorted(staged_root.glob("*/continuity/v1/OWNER.json"))
    if not owner_paths:
        return {"status": "not_present", "owners_relocated": 0, "records_relocated": 0}
    if destination_root.name != "projects" or destination_root.parent.name != "workspace":
        raise ContinuityUnavailable(
            "restored continuity requires a destination ending in workspace/projects"
        )
    destination_home = destination_root.parent.parent
    updates: list[tuple[_Path, bytes]] = []
    source_homes: set[str] = set()
    relocated_owners = 0
    relocated_records = 0
    from odibi_anchor._dispatcher._project import RouteBinding

    for owner_path in owner_paths:
        project_id = owner_path.parents[2].name
        try:
            owner_bytes = owner_path.read_bytes()
            owner = _json.loads(owner_bytes)
        except (OSError, UnicodeDecodeError, _json.JSONDecodeError) as exc:
            raise ContinuityUnavailable("restored continuity owner is unreadable") from exc
        if not isinstance(owner, dict) or set(owner) != {
            "schema_version", "project_id", "anchor_home", "target_root", "artifact_root",
        }:
            raise ContinuityUnavailable("restored continuity owner is malformed")
        if owner_bytes != (_canonical_json(owner) + "\n").encode("utf-8"):
            raise ContinuityUnavailable("restored continuity owner is not canonical")
        source_home = _Path(str(owner["anchor_home"]))
        source_artifact = _Path(str(owner["artifact_root"]))
        if (
            not source_home.is_absolute()
            or owner["project_id"] != project_id
            or source_artifact != source_home / "workspace" / "projects" / project_id
        ):
            raise ContinuityUnavailable(
                "restored continuity owner is not bound to its canonical managed project path"
            )
        source_homes.add(str(source_home))
        destination_artifact = destination_root / project_id
        if source_home == destination_home and source_artifact == destination_artifact:
            continue

        relocated_owner = {
            **owner,
            "anchor_home": str(destination_home),
            "artifact_root": str(destination_artifact),
        }
        updates.append(
            (owner_path, (_canonical_json(relocated_owner) + "\n").encode("utf-8"))
        )
        relocated_owners += 1
        records_root = owner_path.parent
        for record_path in sorted(records_root.glob("*/*/*.json")):
            record = _verified_record(str(record_path))
            record_owner = record.get("owner")
            if not isinstance(record_owner, dict) or _stable_owner(record_owner) != owner:
                raise ContinuityUnavailable(
                    "restored continuity record owner does not match its sentinel"
                )
            matches = []
            for binding_source in ("explicit", "target_match", "legacy_selector"):
                candidate = RouteBinding(
                    project_id=project_id,
                    target_root=str(owner["target_root"]),
                    artifact_root=str(source_artifact),
                    anchor_home=str(source_home),
                    binding_source=binding_source,
                    runtime_instance_id=str(record_owner.get("runtime_instance_id", "")),
                    schema_version=str(record_owner.get("route_schema_version", "")),
                )
                if candidate.fingerprint() == record_owner.get("route_fingerprint"):
                    matches.append(binding_source)
            if len(matches) != 1:
                raise ContinuityUnavailable(
                    "restored continuity record route fingerprint is not reproducible"
                )
            relocated_binding = RouteBinding(
                project_id=project_id,
                target_root=str(owner["target_root"]),
                artifact_root=str(destination_artifact),
                anchor_home=str(destination_home),
                binding_source=matches[0],
                runtime_instance_id=str(record_owner["runtime_instance_id"]),
                schema_version=str(record_owner["route_schema_version"]),
            )
            relocated_record = {
                **record,
                "owner": {
                    **record_owner,
                    "anchor_home": str(destination_home),
                    "artifact_root": str(destination_artifact),
                    "route_fingerprint": relocated_binding.fingerprint(),
                },
            }
            relocated_record.pop("record_sha256", None)
            relocated_record["record_sha256"] = _hashlib.sha256(
                _canonical_json(relocated_record).encode("utf-8")
            ).hexdigest()
            updates.append(
                (record_path, (_canonical_json(relocated_record) + "\n").encode("utf-8"))
            )
            relocated_records += 1

    if len(source_homes) != 1:
        raise ContinuityUnavailable("restored continuity owners disagree on source anchor home")
    for path, content in updates:
        path.write_bytes(content)
    return {
        "status": "relocated" if updates else "unchanged",
        "owners_relocated": relocated_owners,
        "records_relocated": relocated_records,
    }


@_contextmanager
def _continuity_lock(record_path: str):
    lock_path = record_path + ".lock"
    _os.makedirs(_os.path.dirname(record_path), exist_ok=True)
    with open(lock_path, "a+b") as stream:
        try:
            if _os.name == "nt":
                import msvcrt

                stream.seek(0)
                if stream.read(1) == b"":
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if _os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _legacy_backup(root: str) -> dict | None:
    legacy_path = _os.path.join(root, _SESSION_STATE_FILE)
    if not _os.path.isfile(legacy_path):
        return None
    try:
        with open(legacy_path, "rb") as stream:
            content = stream.read()
        legacy = _json.loads(content)
    except (OSError, _json.JSONDecodeError) as exc:
        raise ContinuityUnavailable("legacy session state is malformed and was preserved") from exc
    digest = _hashlib.sha256(content).hexdigest()
    backup = legacy_path + f".pre-continuity-v1.{digest}.bak"
    _write_create_only(backup, content)
    with open(backup, "rb") as stream:
        if _hashlib.sha256(stream.read()).hexdigest() != digest:
            raise ContinuityUnavailable("legacy session state backup verification failed")
    return {"state": legacy, "source_sha256": digest, "backup_path": backup}


def continuity_migration_status(route_binding: object, session_state: object) -> dict:
    """Inspect route-owned continuity and legacy state without creating or changing files."""
    base, sentinel_path, record_path = _continuity_paths(route_binding, session_state)
    expected_owner = _stable_owner(_continuity_owner(route_binding, session_state))
    owner_status = "absent"
    blocker = None
    if _os.path.isfile(sentinel_path):
        try:
            with open(sentinel_path, encoding="utf-8") as stream:
                observed_owner = _json.load(stream)
            owner_status = "exact" if observed_owner == expected_owner else "mismatch"
            if owner_status == "mismatch":
                blocker = "continuity owner sentinel does not match the immutable route"
        except (OSError, _json.JSONDecodeError):
            owner_status = "unreadable"
            blocker = "continuity owner sentinel is unreadable"

    record = None
    if _os.path.isfile(record_path) and blocker is None:
        try:
            record = _verified_record(record_path)
            record_owner = _continuity_owner(route_binding, session_state)
            record_owner["task_window_id"] = record.get("owner", {}).get("task_window_id")
            if record.get("owner") != record_owner:
                blocker = "continuity record owner does not match the immutable route"
        except ContinuityUnavailable as exc:
            blocker = str(exc)

    legacy_path = _os.path.join(route_binding.artifact_root, _SESSION_STATE_FILE)
    legacy = {"status": "not_present"}
    if _os.path.isfile(legacy_path):
        try:
            with open(legacy_path, "rb") as stream:
                content = stream.read()
            payload = _json.loads(content)
            digest = _hashlib.sha256(content).hexdigest()
            backup_path = legacy_path + f".pre-continuity-v1.{digest}.bak"
            legacy = {
                "status": "preserved_ambiguous",
                "source_sha256": digest,
                "backup_path": backup_path,
                "backup_exists": _os.path.isfile(backup_path),
                "task_window_id": payload.get("task_window_id") if isinstance(payload, dict) else None,
            }
        except (OSError, _json.JSONDecodeError):
            legacy = {"status": "malformed_preserved"}
            blocker = blocker or "legacy continuity state is malformed and must be preserved"

    if blocker is not None:
        status, planned_action = "blocked", "fail_closed"
    elif record is not None:
        status, planned_action = "exact", "none"
    elif legacy["status"] != "not_present":
        status, planned_action = "legacy_pending", "preserve_backup_and_initialize_v1_owner"
    else:
        status, planned_action = "uninitialized", "initialize_v1_owner"
    return {
        "schema_status": status,
        "version": _CONTINUITY_SCHEMA_VERSION if record is not None else None,
        "target_version": _CONTINUITY_SCHEMA_VERSION,
        "base_path": base,
        "record_path": record_path,
        "owner_status": owner_status,
        "generation": record.get("generation") if record else None,
        "record_sha256": record.get("record_sha256") if record else None,
        "legacy": legacy,
        "planned_action": planned_action,
        "rollback_eligible": False,
        "rollback_blocker": "continuity evidence is additive and is not deleted automatically",
        "blocker": blocker,
    }


def _load_continuity_state(
    route_binding: object, session_state: object, *, migrate_legacy: bool = True,
) -> dict:
    """Load only this runtime/session's verified route-owned continuity record."""
    _ensure_owner_sentinel(route_binding, session_state)
    _, _, record_path = _continuity_paths(route_binding, session_state)
    if not _os.path.isfile(record_path):
        legacy = _legacy_backup(route_binding.artifact_root) if migrate_legacy else None
        session_state.continuity_generation = 0
        session_state.continuity_record_sha256 = None
        session_state.continuity_status = "legacy_preserved" if legacy else "empty"
        return {
            "state": {}, "record_path": record_path,
            "legacy_migration": (
                {"status": "preserved_ambiguous", **legacy} if legacy else {"status": "not_present"}
            ),
        }
    record = _verified_record(record_path)
    expected_owner = _continuity_owner(route_binding, session_state)
    expected_owner["task_window_id"] = record.get("owner", {}).get("task_window_id")
    if record.get("owner") != expected_owner:
        raise ContinuityUnavailable("continuity record owner mismatch")
    session_state.continuity_generation = record["generation"]
    session_state.continuity_record_sha256 = record["record_sha256"]
    session_state.continuity_status = "ready"
    return {"state": record["state"], "record_path": record_path, "record": record}


def _save_continuity_state(route_binding: object, session_state: object, state: dict) -> dict:
    """Atomically persist one generation without overwriting a stale writer."""
    _ensure_owner_sentinel(route_binding, session_state)
    _, _, record_path = _continuity_paths(route_binding, session_state)
    with _continuity_lock(record_path):
        existing = _verified_record(record_path) if _os.path.isfile(record_path) else None
        expected_generation = session_state.continuity_generation
        expected_digest = session_state.continuity_record_sha256
        observed = (
            (existing["generation"], existing["record_sha256"])
            if existing else (0, None)
        )
        if observed != (expected_generation, expected_digest):
            raise ContinuityUnavailable("stale continuity writer; reload before retrying")
        record = {
            "format": "odibi-anchor-continuity-v1",
            "owner": _continuity_owner(route_binding, session_state),
            "generation": expected_generation + 1,
            "written_at": _datetime.now(_UTC).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            "state": state,
            "prior_record_sha256": expected_digest,
        }
        record["record_sha256"] = _hashlib.sha256(
            _canonical_json(record).encode("utf-8")
        ).hexdigest()
        directory = _os.path.dirname(record_path)
        descriptor, temporary = _tempfile.mkstemp(prefix=".continuity.", dir=directory)
        try:
            with _os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(_canonical_json(record) + "\n")
                stream.flush()
                _os.fsync(stream.fileno())
            _os.replace(temporary, record_path)
            temporary = None
            _fsync_directory(directory)
        finally:
            if temporary is not None:
                with _suppress(OSError):
                    _os.unlink(temporary)
    session_state.continuity_generation = record["generation"]
    session_state.continuity_record_sha256 = record["record_sha256"]
    session_state.continuity_status = "ready"
    return {"record_path": record_path, **record}


def _load_session_state(root: str) -> dict:
    """Load persisted session state from disk, or return empty state."""
    state_path = _os.path.join(root, _SESSION_STATE_FILE)
    try:
        if _os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as f:
                return _json.load(f)
    except Exception as exc:
        import sys as _sys
        print(f"[anchor] session load: {type(exc).__name__}: {exc}", file=_sys.stderr)
    return {}


def _save_session_state(root: str, state: dict, *, strict: bool = False) -> None:
    """Atomically persist session state, optionally making durability failures fatal."""
    state_path = _os.path.join(root, _SESSION_STATE_FILE)
    directory = _os.path.dirname(state_path) or "."
    temporary = None
    try:
        fd, temporary = _tempfile.mkstemp(prefix=f".{_SESSION_STATE_FILE}.", dir=directory)
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(state, f, indent=2)
            f.write("\n")
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(temporary, state_path)
        temporary = None
        if _CAN_FSYNC_DIRECTORY:
            directory_fd = _os.open(directory, _os.O_RDONLY)
            try:
                _os.fsync(directory_fd)
            finally:
                _os.close(directory_fd)
    except Exception as exc:
        if strict:
            raise RuntimeError("session state could not be persisted atomically") from exc
        import sys as _sys

        print(f"[anchor] session save: {type(exc).__name__}: {exc}", file=_sys.stderr)
    finally:
        if temporary is not None:
            with _suppress(OSError):
                _os.unlink(temporary)


# ─── Config management ────────────────────────────────────────────────────────

def _config(root: str, *args, **kwargs) -> dict:
    """View or edit anti-pattern suppress configuration.

    Usage:
        anchor("config")                                    # View current config
        anchor("config", suppress_category="performance")   # Add category suppression
        anchor("config", unsuppress_category="performance") # Remove category suppression
        anchor("config", suppress_id="todo_fixme_hack")     # Add specific ID suppression
        anchor("config", unsuppress_id="todo_fixme_hack")   # Remove specific ID suppression
        anchor("config", file_override="tests/**",          # Add file-level override
            suppress_categories=["performance"])
        anchor("config", remove_override="tests/**")        # Remove file-level override

    Config is persisted to .anchor_config.json at the project root.
    """
    config_path = _os.path.join(root, ".anchor_config.json")

    # Load existing config (or empty default)
    if _os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as _f:
            config = _json.load(_f)
    else:
        config = {}

    ap = config.setdefault("anti_patterns", {})
    ap.setdefault("suppress_categories", [])
    ap.setdefault("suppress_ids", [])
    ap.setdefault("file_overrides", {})

    mutated = False

    # --- Mutations ---
    if "suppress_category" in kwargs:
        cat = kwargs["suppress_category"]
        if cat not in ap["suppress_categories"]:
            ap["suppress_categories"].append(cat)
            ap["suppress_categories"].sort()
            mutated = True

    if "unsuppress_category" in kwargs:
        cat = kwargs["unsuppress_category"]
        if cat in ap["suppress_categories"]:
            ap["suppress_categories"].remove(cat)
            mutated = True

    if "suppress_id" in kwargs:
        pid = kwargs["suppress_id"]
        if pid not in ap["suppress_ids"]:
            ap["suppress_ids"].append(pid)
            ap["suppress_ids"].sort()
            mutated = True

    if "unsuppress_id" in kwargs:
        pid = kwargs["unsuppress_id"]
        if pid in ap["suppress_ids"]:
            ap["suppress_ids"].remove(pid)
            mutated = True

    if "file_override" in kwargs:
        glob_pattern = kwargs["file_override"]
        override = {}
        if "suppress_categories" in kwargs:
            override["suppress_categories"] = kwargs["suppress_categories"]
        if "suppress_ids" in kwargs:
            override["suppress_ids"] = kwargs["suppress_ids"]
        if override:
            ap["file_overrides"][glob_pattern] = override
            mutated = True

    if "remove_override" in kwargs:
        glob_pattern = kwargs["remove_override"]
        if glob_pattern in ap["file_overrides"]:
            del ap["file_overrides"][glob_pattern]
            mutated = True

    # --- Persist if changed ---
    if mutated:
        with open(config_path, "w", encoding="utf-8") as _f:
            _json.dump(config, _f, indent=2)
            _f.write("\n")

    # --- Build output ---
    all_categories = [
        "performance", "convention", "correctness", "migration",
        "reliability", "security", "portability", "compatibility",
        "data_safety", "maintainability", "observability",
    ]

    active_categories = [c for c in all_categories if c not in ap["suppress_categories"]]
    suppressed_categories = ap["suppress_categories"]

    findings = []
    if suppressed_categories:
        findings.append(f"Suppressed categories: {', '.join(suppressed_categories)}")
    else:
        findings.append("No categories suppressed (all 11 active)")
    if ap["suppress_ids"]:
        findings.append(f"Suppressed IDs: {', '.join(ap['suppress_ids'])}")
    if ap["file_overrides"]:
        for glob, override in ap["file_overrides"].items():
            parts = []
            if override.get("suppress_categories"):
                parts.append(f"categories: {override['suppress_categories']}")
            if override.get("suppress_ids"):
                parts.append(f"ids: {override['suppress_ids']}")
            findings.append(f"  {glob} → {', '.join(parts)}")

    summary = (
        f"Config: {len(active_categories)}/11 categories active, "
        f"{len(ap['suppress_ids'])} IDs suppressed, "
        f"{len(ap['file_overrides'])} file overrides"
        f"{' (updated)' if mutated else ''}"
    )

    ctx = build_base_context(
        kind="config_context",
        subject="anti_patterns",
        summary=summary,
        metrics={
            "active_categories": len(active_categories),
            "suppressed_categories": len(suppressed_categories),
            "suppressed_ids": len(ap["suppress_ids"]),
            "file_overrides": len(ap["file_overrides"]),
            "config_path": config_path,
            "mutated": mutated,
        },
        findings=findings,
        risks=[],
        samples={},
        suggested_next_actions=[
            "anchor('config', suppress_category='...')" if not mutated else "Config updated — changes take effect on next anchor('safe') call",
        ],
        active_categories=active_categories,
        suppressed_categories=suppressed_categories,
        suppressed_ids=ap["suppress_ids"],
        file_overrides=ap["file_overrides"],
        all_categories=all_categories,
    )
    if kwargs.get("output_format") == "markdown":
        lines = ["# Anti-Pattern Config\n"]
        lines.append(f"**{summary}**\n")
        lines.append("## Active Categories\n")
        for c in active_categories:
            lines.append(f"- ✓ {c}")
        if suppressed_categories:
            lines.append("\n## Suppressed Categories\n")
            for c in suppressed_categories:
                lines.append(f"- ✗ {c}")
        if ap["suppress_ids"]:
            lines.append("\n## Suppressed IDs\n")
            for pid in ap["suppress_ids"]:
                lines.append(f"- {pid}")
        if ap["file_overrides"]:
            lines.append("\n## File Overrides\n")
            lines.append("| Pattern | Suppressed |")
            lines.append("| --- | --- |")
            for glob_pat, override in ap["file_overrides"].items():
                parts = []
                if override.get("suppress_categories"):
                    parts.append(f"categories: {override['suppress_categories']}")
                if override.get("suppress_ids"):
                    parts.append(f"ids: {override['suppress_ids']}")
                lines.append(f"| {glob_pat} | {', '.join(parts)} |")
        return "\n".join(lines)
    return ctx


# ─── Session file registry ────────────────────────────────────────────────────

def _session_files(session_context: dict, **kwargs) -> dict:
    """Return the current session file registry (what anchor('gate') would auto-use).

    Args:
        session_context: Dict with environment metadata (cluster, notebook path, etc.)
    """
    output_format = kwargs.pop("output_format", "dict")

    total_changed = len(_SESSION_FILES_CHANGED)
    total_created = len(_SESSION_FILES_CREATED)
    total_actions = len(_SESSION_TIMINGS)
    total_time_ms = round(sum(t["elapsed_ms"] for t in _SESSION_TIMINGS), 1)

    ctx = build_base_context(
        kind="session_files",
        subject="session_registry",
        summary=f"Session: {total_changed} files changed, {total_created} created, {total_actions} actions ({total_time_ms}ms)",
        metrics={
            "total_changed": total_changed,
            "total_created": total_created,
            "total_actions": total_actions,
            "total_time_ms": total_time_ms,
        },
        findings=[f"Changed: {f}" for f in sorted(_SESSION_FILES_CHANGED)],
        risks=[],
        samples={},
        suggested_next_actions=[
            "MUST: Run anchor('gate') to verify session compliance",
            "MUST: Run anchor('learning', 'capture', ...) for reusable findings, then anchor('learning', 'assess', outcome=...)",
            "SHOULD: Review the complete diff before gate.",
        ],
        files_changed=sorted(_SESSION_FILES_CHANGED),
        files_created=sorted(_SESSION_FILES_CREATED),
        timings=_SESSION_TIMINGS,
        session_context=session_context,
    )
    if output_format == "markdown":
        lines = ["# Session Files\n"]
        lines.append(f"**{ctx['summary']}**\n")
        lines.append("## Metrics\n")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for k, v in ctx["metrics"].items():
            lines.append(f"| {k} | {v} |")
        if ctx.get("files_changed"):
            lines.append("\n## Changed Files\n")
            for f in ctx["files_changed"]:
                lines.append(f"- {f}")
        if ctx.get("files_created"):
            lines.append("\n## Created Files\n")
            for f in ctx["files_created"]:
                lines.append(f"- {f}")
        if ctx.get("suggested_next_actions"):
            lines.append("\n## Next Actions\n")
            for a in ctx["suggested_next_actions"]:
                lines.append(f"- {a}")
        return "\n".join(lines)
    return ctx


# ─── Session diff ─────────────────────────────────────────────────────────────

def _session_diff_action(root: str, *args, **kwargs):
    """Wrapper for session diff that handles output_format."""
    output_format = kwargs.pop("output_format", "dict")
    target = kwargs.get("target", args[0] if args else None)
    stat = kwargs.get("stat", False)
    result = _get_session_diff(target=target, stat=stat, root=root)
    if output_format == "markdown":
        lines = [f"# Session Diff: {result['subject']}\n"]
        lines.append(f"**Summary:** {result['summary']}\n")
        lines.append("## Metrics\n")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for k, v in result["metrics"].items():
            lines.append(f"| {k} | {v} |")
        samples = result.get("samples", {})
        diffstat = samples.get("diffstat") or samples.get("per_file", {})
        if diffstat:
            lines.append("\n## Files\n")
            for path, info in diffstat.items():
                if isinstance(info, dict):
                    status = info.get("status", "modified")
                    adds = info.get("additions", 0)
                    dels = info.get("deletions", 0)
                    lines.append(f"- **{path}** ({status}) +{adds}/-{dels}")
                    if not stat and "diff" in info and info["diff"]:
                        lines.append(f"```diff\n{info['diff']}\n```")
        return "\n".join(lines)
    return result


def _touched_action(path: str, root: str, *, created: bool = False, **_kwargs) -> dict:
    """Register a file as changed (or created) during this session.

    Delegates to the shared _session_state singleton so that both
    the exec'd namespace and safe_change_context share the same state.
    Includes cross-project detection for files outside the current ROOT.
    """
    result = _session_touched(path, created=created, root=root)

    # Cross-project warning: detect files outside current ROOT
    _abs_path = path if _os.path.isabs(path) else _os.path.join(str(root), path)
    if not _abs_path.startswith(str(root)):
        from odibi_anchor._dispatcher._boot import _discover_project_roots
        _detected_root = None
        for _candidate in _discover_project_roots():
            if _abs_path.startswith(_candidate):
                _detected_root = _candidate
                break
        _warning = (
            f"File '{path}' is outside current project root ({root}). "
        )
        if _detected_root:
            _warning += (
                f"Detected project: {_detected_root}. "
                f"Consider: re-bootstrap with _AGENT_ROOT='{_detected_root}'"
            )
        else:
            _warning += "Could not detect a project root for this path."
        result["cross_project_warning"] = _warning

    from odibi_anchor._utils._session_state import _SESSION_STATE

    profile = _SESSION_STATE.active_task_profile
    if (
        _SESSION_STATE.task_window_id
        and profile is not None
        and profile.execution_mode == "source_change"
    ):
        try:
            relative = _os.path.relpath(_os.path.abspath(_abs_path), _os.path.abspath(str(root)))
        except ValueError:
            relative = ".."
        if relative != ".." and not relative.startswith(".." + _os.sep):
            from odibi_anchor._dispatcher._boot import _ENV
            from odibi_anchor.codebase._adopted_dirty import record_touched_path

            result["durable_task_touch"] = record_touched_path(
                _ENV["memory_db"], task_window_id=_SESSION_STATE.task_window_id,
                touched_path=relative,
            )

    return result
