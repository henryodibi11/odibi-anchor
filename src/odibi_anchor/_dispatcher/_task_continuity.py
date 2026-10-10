"""Per-task process continuity that survives a dispatcher restart and ``task_rebind``.

A self-hosted dispatcher becomes stale whenever its own source changes, and a fresh
process loses the in-memory known_bad, touched, skill and spec-link state of the
open task. This module persists that state for one exact accepted task window and
restores only entries whose bytes are unchanged:

- touched registrations when the file's current sha256 equals the registered bytes;
- the known_bad check when every checked path still has its checked bytes or the
  bytes registered by this task's own ``touched``;
- skill registrations when the resolved skill content sha256 is unchanged;
- the spec link when the spec file sha256 is unchanged.

Records are checksummed and bound to the task window and its accepted-record sha256,
so they never transfer to another task, record or set of bytes. Restoration grants no
authority: task authority still comes only from ``rebind_latest_open_task`` and the
source-revision guard is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_DIRECTORY = ".anchor_task_continuity"
_MAX_BASELINE_BYTES = 1024 * 1024


def _store(memory_db: str | os.PathLike[str], task_window_id: str) -> Path:
    if not isinstance(task_window_id, str) or not task_window_id.startswith("ltw_") or "/" in task_window_id:
        raise ValueError("task continuity requires an exact task window id")
    return Path(memory_db).expanduser().resolve().parent / _DIRECTORY / task_window_id


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def accepted_record_sha256(memory_db: str | os.PathLike[str], task_window_id: str) -> str | None:
    """Return the canonical sha256 of the persisted accepted record, if it exists."""
    from odibi_anchor.codebase._task_authority import (
        _canonical as authority_canonical,
    )
    from odibi_anchor.codebase._task_authority import (
        _connect,
        _load_verified_record,
        _verify_schema,
    )

    if not Path(memory_db).expanduser().is_file():
        return None
    connection = _connect(memory_db, read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accepted_task_records" not in tables:
            return None
        _verify_schema(connection)
        row = connection.execute(
            "SELECT * FROM accepted_task_records WHERE task_window_id=?", (task_window_id,),
        ).fetchone()
        if row is None:
            return None
        return _sha256_bytes(authority_canonical(_load_verified_record(row)).encode())
    finally:
        connection.close()


def _read(store: Path) -> dict[str, Any] | None:
    path = store / "record.json"
    if not path.is_file():
        return None
    envelope = json.loads(path.read_text(encoding="utf-8"))
    record = envelope.get("record") if isinstance(envelope, dict) else None
    if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION
            or envelope.get("record_sha256") != _sha256_bytes(_canonical(record).encode())):
        raise ValueError("task continuity record integrity mismatch")
    return record


def _write(store: Path, record: dict[str, Any]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    payload = {"record": record, "record_sha256": _sha256_bytes(_canonical(record).encode())}
    temporary = store / "record.json.tmp"
    temporary.write_text(_canonical(payload), encoding="utf-8")
    os.replace(temporary, store / "record.json")


def _relative(path: str, target_root: Path) -> str:
    from odibi_anchor._utils._session_state import canonical_session_path

    return canonical_session_path(path, str(target_root))


def record_continuity(memory_db, *, session_state, action, kwargs, result, diff_baselines) -> None:
    """Merge one successful known_bad, touched, skill_loaded or spec call into the record."""
    task_window_id = session_state.task_window_id
    target_root = Path(session_state.target_root or session_state.artifact_root).resolve()
    accepted = accepted_record_sha256(memory_db, task_window_id)
    if accepted is None:
        return  # Legacy or unpersisted task: nothing durable to bind to.
    store = _store(memory_db, task_window_id)
    try:
        record = _read(store)
    except (OSError, ValueError):
        record = None  # A damaged record is replaced, never trusted.
    if record is None or record.get("accepted_record_sha256") != accepted:
        record = {"schema_version": SCHEMA_VERSION, "task_window_id": task_window_id,
                  "accepted_record_sha256": accepted, "target_root": str(target_root),
                  "known_bad": {}, "touched": {}, "skills": {}, "spec": None}
    if action == "known_bad":
        paths = kwargs.get("changed_files") or ([kwargs["target"]] if kwargs.get("target") else [])
        for item in paths if isinstance(paths, (list, tuple)) else ():
            relative = _relative(str(item), target_root)
            record["known_bad"][relative] = _file_sha256(target_root / relative)
    elif action == "touched" and isinstance(result.get("registered"), str) and "managed_artifact" not in result:
        relative = result["registered"]
        baseline = diff_baselines.get(relative)
        baseline_sha256 = None
        if isinstance(baseline, str) and len(baseline.encode("utf-8")) <= _MAX_BASELINE_BYTES:
            baseline_sha256 = _sha256_bytes(baseline.encode("utf-8"))
            blob = store / "baselines" / baseline_sha256
            if not blob.is_file():
                blob.parent.mkdir(parents=True, exist_ok=True)
                blob.write_text(baseline, encoding="utf-8")
        elif baseline is not None:
            baseline_sha256 = "unavailable"
        record["touched"][relative] = {
            "sha256": _file_sha256(target_root / relative),
            "created": bool(result.get("created")), "baseline_sha256": baseline_sha256,
        }
    elif action == "skill_loaded" and isinstance(result.get("registered"), str):
        record["skills"][result["registered"]] = result.get("content_sha256")
    elif action == "spec":
        linked = getattr(session_state, "linked_spec", None)
        spec_path = _spec_path(session_state, linked)
        record["spec"] = None if spec_path is None else {
            "linked_spec": linked,
            "persisted_spec_name": getattr(session_state, "persisted_spec_name", None),
            "reviewed_spec_name": getattr(session_state, "reviewed_spec_name", None),
            "spec_review_rating": getattr(session_state, "spec_review_rating", None),
            "spec_sha256": _file_sha256(spec_path),
        }
    _write(store, record)


def _spec_path(session_state, name) -> Path | None:
    if not name or not getattr(session_state, "artifact_root", None):
        return None
    from odibi_anchor.codebase._spec_parser import find_spec

    found = find_spec(Path(session_state.artifact_root) / "specs", name)
    raw = (found or {}).get("raw_path")
    return Path(raw) if raw else None


def restore_continuity(memory_db, *, session_state, files_changed, files_created, diff_baselines,
                       timings, skill_content_sha256) -> dict[str, Any]:
    """Restore unchanged entries for the rebound task; report every entry not restored."""
    task_window_id = session_state.task_window_id
    store = _store(memory_db, task_window_id)
    try:
        record = _read(store)
    except (OSError, ValueError) as exc:
        return {"status": "rejected", "reason": f"{type(exc).__name__}: {exc}", "restored": {}, "not_restored": []}
    if record is None:
        return {"status": "none", "restored": {}, "not_restored": []}
    target_root = Path(session_state.target_root or session_state.artifact_root).resolve()
    if (record.get("task_window_id") != task_window_id
            or record.get("accepted_record_sha256") != accepted_record_sha256(memory_db, task_window_id)
            or record.get("target_root") != str(target_root)):
        return {"status": "rejected", "reason": "record is bound to a different task, record or target",
                "restored": {}, "not_restored": []}
    restored: dict[str, list[str]] = {"touched": [], "known_bad": [], "skills": [], "spec": []}
    not_restored: list[dict[str, str]] = []
    databricks = getattr(getattr(session_state, "task_repository_baseline", None), "evidence_kind", None) \
        == "databricks_git_folder"
    current: dict[str, str | None] = {}

    def sha(relative: str) -> str | None:
        if relative not in current:
            current[relative] = _file_sha256(target_root / relative)
        return current[relative]

    touched_ok: set[str] = set()
    for relative, entry in sorted(record["touched"].items()):
        baseline_sha256 = entry.get("baseline_sha256")
        baseline_text = None
        if baseline_sha256 not in (None, "unavailable"):
            blob = store / "baselines" / baseline_sha256
            text = blob.read_text(encoding="utf-8") if blob.is_file() else None
            if text is not None and _sha256_bytes(text.encode("utf-8")) == baseline_sha256:
                baseline_text = text
                diff_baselines.setdefault(relative, text)  # Pre-edit bytes are bound by their own hash.
        if databricks:
            not_restored.append({"kind": "touched", "name": relative,
                                 "reason": "databricks write acknowledgement requires touched again"})
        elif sha(relative) != entry.get("sha256"):
            not_restored.append({"kind": "touched", "name": relative, "reason": "content_changed"})
        elif baseline_sha256 is not None and baseline_text is None:
            not_restored.append({"kind": "touched", "name": relative, "reason": "baseline_unavailable"})
        else:
            files_changed.add(relative)
            if entry.get("created"):
                files_created.add(relative)
            touched_ok.add(relative)
            restored["touched"].append(relative)
    known_bad = record["known_bad"]
    stale = sorted(
        relative for relative, checked in known_bad.items()
        if sha(relative) != checked and not (
            relative in touched_ok and sha(relative) == record["touched"][relative].get("sha256"))
    )
    if known_bad and not stale:
        timings.append({"action": "known_bad", "elapsed_ms": 0.0, "error": None, "passed": True,
                        "synthetic": "task_continuity"})
        restored["known_bad"] = sorted(known_bad)
    elif stale:
        not_restored.append({"kind": "known_bad", "name": ", ".join(stale[:10]), "reason": "content_changed"})
    for name, expected in sorted(record["skills"].items()):
        try:
            observed = skill_content_sha256(name)
        except (OSError, ValueError, KeyError) as exc:
            observed = f"unavailable:{type(exc).__name__}"
        if expected and observed == expected:
            session_state.skills_loaded.add(name)
            restored["skills"].append(name)
        else:
            not_restored.append({"kind": "skill", "name": name, "reason": "content_changed"})
    spec = record.get("spec")
    if spec:
        path = _spec_path(session_state, spec.get("linked_spec"))
        if path is not None and _file_sha256(path) == spec.get("spec_sha256"):
            session_state.linked_spec = spec["linked_spec"]
            session_state.persisted_spec_name = spec.get("persisted_spec_name")
            session_state.spec_persisted = spec.get("persisted_spec_name") == spec["linked_spec"]
            session_state.reviewed_spec_name = spec.get("reviewed_spec_name")
            session_state.spec_review_rating = spec.get("spec_review_rating")
            restored["spec"].append(spec["linked_spec"])
        else:
            not_restored.append({"kind": "spec", "name": str(spec.get("linked_spec")), "reason": "content_changed"})
    return {"status": "restored", "restored": restored, "not_restored": not_restored}


def discard_continuity(memory_db, task_window_id: str) -> None:
    """Remove the continuity record of a task window that reached terminal closure."""
    store = _store(memory_db, task_window_id)
    if store.is_dir():
        shutil.rmtree(store)
