"""Installed, storage-derived pytest verification for exact memory claims.

The verifier accepts a memory identity, never caller-authored selectors or
results.  Eligible selectors come from the immutable structured-learning
evidence that produced the candidate.  Runs are append-only and become
attestation inputs only after their distinct task has a terminal record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DOMAIN = "memory_verifier"
SCHEMA_VERSION = 1
PROVIDER = "odibi-anchor-pytest"
PYTHON_CALL_PROVIDER = "odibi-anchor-python-call"
PROVIDER_VERSION = "1"
PYTHON_CALL_PREFIX = "PythonCallResultV1:"
PYTHON_CALL_MANIFEST = ".odibi-anchor/memory-verifiers.json"
_SNAPSHOT_LOCK = threading.Lock()
_PYTHON_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CALLABLE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")

_DDL = (
    "CREATE TABLE memory_verifier_runs (run_id TEXT PRIMARY KEY,"
    "memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,"
    "claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64),"
    "project_id TEXT NOT NULL,trust_domain TEXT NOT NULL,"
    "task_window_id TEXT NOT NULL REFERENCES accepted_task_records(task_window_id) "
    "ON DELETE RESTRICT,source_task_window_id TEXT NOT NULL "
    "REFERENCES terminal_task_records(task_window_id) ON DELETE RESTRICT,"
    "selector_json TEXT NOT NULL,check_command_sha256 TEXT NOT NULL "
    "CHECK(length(check_command_sha256)=64),check_result_sha256 TEXT NOT NULL "
    "CHECK(length(check_result_sha256)=64),artifact_sha256 TEXT NOT NULL "
    "CHECK(length(artifact_sha256)=64),source_snapshot_sha256 TEXT NOT NULL "
    "CHECK(length(source_snapshot_sha256)=64),source_head_sha TEXT NOT NULL "
    "CHECK(length(source_head_sha) IN (40,64)),verifier_provider TEXT NOT NULL,"
    "verifier_version TEXT NOT NULL,derivation_id TEXT NOT NULL,"
    "evidence_identity TEXT NOT NULL,result_state TEXT NOT NULL "
    "CHECK(result_state IN ('passed','failed','unavailable')),"
    "contradiction_status TEXT NOT NULL "
    "CHECK(contradiction_status IN ('clear','contradicted','unavailable')),"
    "payload_json TEXT NOT NULL,started_at TEXT NOT NULL,completed_at TEXT NOT NULL,"
    "UNIQUE(task_window_id,evidence_identity))",
    "CREATE INDEX idx_memory_verifier_runs_memory ON memory_verifier_runs(memory_id,completed_at,run_id)",
    "CREATE INDEX idx_memory_verifier_runs_evidence ON memory_verifier_runs(evidence_identity,completed_at,run_id)",
    "CREATE TRIGGER memory_verifier_runs_no_update BEFORE UPDATE ON memory_verifier_runs BEGIN SELECT RAISE(ABORT,'memory verifier runs are immutable'); END",
    "CREATE TRIGGER memory_verifier_runs_no_delete BEFORE DELETE ON memory_verifier_runs BEGIN SELECT RAISE(ABORT,'memory verifier runs are immutable'); END",
)
SCHEMA_TEXT = ";\n".join(_DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def canonical_pytest_result_claim(*, project_id: str, selectors: list[str]) -> str:
    """Build the only natural-language-free claim this verifier can prove."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    if (
        not isinstance(selectors, list)
        or not 1 <= len(selectors) <= 5
        or not all(isinstance(item, str) and item.strip() for item in selectors)
        or selectors != sorted(set(selectors))
    ):
        raise ValueError("selectors must contain one through five unique sorted strings")
    return "PytestResultV1:" + _json({
        "outcome": "passed", "project_id": project_id, "selectors": selectors,
    })


def _validate_json_value(value: Any, label: str) -> None:
    try:
        encoded = _json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON data") from exc
    if len(encoded.encode("utf-8")) > 16_000:
        raise ValueError(f"{label} exceeds the bounded JSON size")


def _validated_callable(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"id", "module", "qualname", "source"}:
        raise ValueError("callable must contain id, module, qualname, and source")
    if not isinstance(value["id"], str) or not _CALLABLE_ID.fullmatch(value["id"]):
        raise ValueError("callable id is invalid")
    for field in ("module", "qualname"):
        candidate = value[field]
        if (
            not isinstance(candidate, str)
            or not candidate
            or not all(_PYTHON_NAME.fullmatch(part) for part in candidate.split("."))
        ):
            raise ValueError(f"callable {field} is invalid")
    source = value["source"]
    source_path = PurePosixPath(source) if isinstance(source, str) else None
    if (
        source_path is None
        or source_path.is_absolute()
        or source_path.suffix != ".py"
        or any(part in {"", ".", ".."} for part in source_path.parts)
        or source != source_path.as_posix()
    ):
        raise ValueError("callable source must be a repository-relative Python path")
    module_path = PurePosixPath(*value["module"].split(".")).with_suffix(".py")
    if source_path not in {module_path, PurePosixPath("src") / module_path}:
        raise ValueError("callable source does not match its importable module")
    return {key: value[key] for key in ("id", "module", "qualname", "source")}


def canonical_python_call_result_claim(
    *, project_id: str, callable_spec: dict[str, str], args: list[Any],
    kwargs: dict[str, Any], assertions: list[dict[str, Any]],
) -> str:
    """Build a closed claim about exact outputs from a source-allowlisted Python call."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    target = _validated_callable(callable_spec)
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ValueError("args and kwargs must be JSON list/object values")
    if not all(isinstance(key, str) and key for key in kwargs):
        raise ValueError("kwargs keys must be non-empty strings")
    _validate_json_value(args, "args")
    _validate_json_value(kwargs, "kwargs")
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 8:
        raise ValueError("assertions must contain one through eight exact comparisons")
    normalized = []
    for assertion in assertions:
        if not isinstance(assertion, dict) or set(assertion) != {"path", "equals"}:
            raise ValueError("each assertion must contain path and equals")
        pointer = assertion["path"]
        if (
            not isinstance(pointer, str)
            or not pointer.startswith("/")
            or len(pointer) > 256
            or len(pointer.split("/")) > 10
        ):
            raise ValueError("assertion path must be a bounded JSON pointer")
        _validate_json_value(assertion["equals"], "assertion value")
        normalized.append({"path": pointer, "equals": assertion["equals"]})
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise ValueError("assertions must be sorted by path")
    if len({item["path"] for item in normalized}) != len(normalized):
        raise ValueError("assertion paths must be unique")
    content = PYTHON_CALL_PREFIX + _json({
        "args": args,
        "assertions": normalized,
        "callable": target,
        "kwargs": kwargs,
        "outcome": "returned",
        "project_id": project_id,
    })
    if len(content) > 1000:
        raise ValueError("PythonCallResultV1 claim exceeds memory content limit")
    return content


def _parse_python_call_claim(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.removeprefix(PYTHON_CALL_PREFIX))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("PythonCallResultV1 claim is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "args", "assertions", "callable", "kwargs", "outcome", "project_id",
    }:
        raise ValueError("PythonCallResultV1 claim has unexpected fields")
    canonical = canonical_python_call_result_claim(
        project_id=payload["project_id"], callable_spec=payload["callable"],
        args=payload["args"], kwargs=payload["kwargs"], assertions=payload["assertions"],
    )
    if content != canonical or payload["outcome"] != "returned":
        raise ValueError("PythonCallResultV1 claim is not canonical")
    return payload


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def _connection(path: str | Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    resolved = Path(path).expanduser().resolve()
    target = f"{resolved.as_uri()}?mode=ro" if read_only else str(resolved)
    connection = connect_shared_memory(
        target,
        owner_key_kind="task_window_id,evidence_identity",
        uri=read_only,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("memory verifier foreign keys disabled")
    if not read_only:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    try:
        yield connection
    finally:
        connection.close()


def _expected_objects() -> list[tuple[Any, ...]]:
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
        expected.execute("CREATE TABLE accepted_task_records (task_window_id TEXT PRIMARY KEY)")
        for statement in _DDL:
            expected.execute(statement)
        return [
            tuple(row)
            for row in expected.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name LIKE 'memory_verifier_%' OR name LIKE 'idx_memory_verifier_%' "
                "ORDER BY type,name"
            )
        ]
    finally:
        expected.close()


def _verify_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
    ).fetchone()
    if tuple(version or ()) != (SCHEMA_VERSION, SCHEMA_SHA256):
        raise RuntimeError("memory verifier schema version/checksum mismatch")
    expected = _expected_objects()
    names = [row[1] for row in expected]
    placeholders = ",".join("?" for _ in names)
    actual = [
        tuple(row)
        for row in connection.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) "
            "ORDER BY type,name",
            names,
        )
    ]
    owned = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='memory_verifier_runs' "
            "AND sql IS NOT NULL"
        )
    }
    if actual != expected or owned != set(names):
        raise RuntimeError("memory verifier schema checksum mismatch")


def initialize_schema(path: str | Path) -> dict[str, Any]:
    """Create or exactly verify the additive verifier-run schema."""
    with _connection(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS anchor_schema_versions (domain TEXT PRIMARY KEY, "
                "version INTEGER NOT NULL CHECK(version>0),schema_sha256 TEXT NOT NULL "
                "CHECK(length(schema_sha256)=64),applied_at TEXT NOT NULL)"
            )
            version = connection.execute(
                "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
            ).fetchone()
            if version is not None and tuple(version) != (SCHEMA_VERSION, SCHEMA_SHA256):
                raise RuntimeError("memory verifier schema version/checksum mismatch")
            if version is None:
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                    (DOMAIN, SCHEMA_VERSION, SCHEMA_SHA256, _now()),
                )
            _verify_schema(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"domain": DOMAIN, "version": SCHEMA_VERSION, "schema_sha256": SCHEMA_SHA256}


def _eligible_claim(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str,
) -> tuple[sqlite3.Row, str, str, str, dict[str, Any]]:
    from odibi_anchor.codebase._memory_promotion import _claim
    from odibi_anchor.codebase.structured_learning_context import _verify as verify_learning

    memory = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if memory is None:
        raise ValueError("memory entry not found")
    if memory["project"] != project_id:
        raise ValueError("memory belongs to a different project/trust boundary")
    if memory["status"] not in {"candidate", "active", "confirmed"}:
        raise ValueError("installed verifier accepts candidate, active, or confirmed memories only")
    try:
        evidence = json.loads(memory["evidence"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("memory evidence contains invalid JSON") from exc
    item_id = evidence.get("learning_item_id")
    source_task = evidence.get("task_window_id")
    evidence_hashes = evidence.get("evidence_ref_sha256")
    if (
        not isinstance(item_id, str)
        or not isinstance(source_task, str)
        or not isinstance(evidence_hashes, list)
        or not all(isinstance(item, str) and len(item) == 64 for item in evidence_hashes)
    ):
        raise ValueError("memory is not an eligible assessed structured-learning claim")
    verify_learning(connection)
    references = connection.execute(
        "SELECT reference_type,reference,reference_sha256 FROM learning_evidence_refs "
        "WHERE item_id=? ORDER BY reference_type,reference",
        (item_id,),
    ).fetchall()
    claim_sha256 = _sha256(_claim(memory))
    bound = [row for row in references if row["reference_sha256"] in evidence_hashes]
    if memory["content"].startswith(PYTHON_CALL_PREFIX):
        payload = _parse_python_call_claim(memory["content"])
        if payload["project_id"] != project_id:
            raise ValueError("PythonCallResultV1 project does not match memory trust boundary")
        source = payload["callable"]["source"]
        file_refs = [
            row["reference"].split("#", 1)[0]
            for row in bound if row["reference_type"] == "file"
        ]
        if source not in file_refs:
            raise ValueError("PythonCallResultV1 has no bound source-file evidence")
        return memory, claim_sha256, source_task, PYTHON_CALL_PROVIDER, payload

    selectors = [row["reference"] for row in bound if row["reference_type"] == "test"]
    if not selectors:
        raise ValueError("memory has no bound test evidence selectors")
    if len(selectors) > 5:
        raise ValueError("memory has more than five bound test evidence selectors")
    expected_content = canonical_pytest_result_claim(
        project_id=project_id, selectors=selectors,
    )
    if memory["content"] != expected_content:
        raise ValueError("unbound natural-language claims require authenticated human authority")
    return memory, claim_sha256, source_task, PROVIDER, {"selectors": selectors}


def _source_snapshot(target_root: str | Path, configured_target_ref: str) -> tuple[str, str, dict]:
    from odibi_anchor._repository_snapshot import capture_repository_snapshot

    with _SNAPSHOT_LOCK:
        snapshot = capture_repository_snapshot(target_root, configured_target_ref)
    payload = {
        "format": "odibi-anchor-memory-verifier-source-v1",
        "branch": snapshot.branch,
        "head_sha": snapshot.head_sha,
        "target_sha": snapshot.target_sha,
        "index_fingerprint": snapshot.provenance["index_fingerprint"],
        "worktree_fingerprint": snapshot.provenance["worktree_fingerprint"],
        "changed_paths": list(snapshot.changed_paths),
    }
    return _sha256(payload), snapshot.head_sha, payload


def _tracked_safe_path(target_root: Path, relative: str, label: str) -> Path:
    candidate = target_root / relative
    if (
        not candidate.is_file()
        or candidate.is_symlink()
        or not candidate.resolve().is_relative_to(target_root)
    ):
        raise ValueError(f"{label} is unavailable or unsafe")
    tracked = subprocess.run(
        ["git", "-C", str(target_root), "ls-files", "--error-unmatch", "--", relative],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if tracked.returncode != 0:
        raise ValueError(f"{label} must be tracked by the target repository")
    return candidate.resolve()


def _verified_python_callable(target_root: Path, callable_spec: dict[str, str]) -> Path:
    manifest_path = _tracked_safe_path(target_root, PYTHON_CALL_MANIFEST, "verifier manifest")
    try:
        if manifest_path.stat().st_size > 256_000:
            raise ValueError("verifier manifest exceeds size limit")
        raw = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("verifier manifest is unreadable or malformed") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "callables"}
        or manifest["format"] != "odibi-anchor-memory-verifiers-v1"
        or not isinstance(manifest["callables"], list)
        or not 1 <= len(manifest["callables"]) <= 64
    ):
        raise ValueError("verifier manifest schema is invalid")
    entries = []
    for entry in manifest["callables"]:
        if not isinstance(entry, dict) or set(entry) != {
            "id", "module", "qualname", "source", "args", "kwargs", "assertions",
        }:
            raise ValueError("verifier manifest entry schema is invalid")
        callable_entry = _validated_callable({
            key: entry[key] for key in ("id", "module", "qualname", "source")
        })
        canonical_python_call_result_claim(
            project_id="manifest-validation", callable_spec=callable_entry,
            args=entry["args"], kwargs=entry["kwargs"], assertions=entry["assertions"],
        )
        entries.append({
            "callable": callable_entry, "args": entry["args"], "kwargs": entry["kwargs"],
            "assertions": entry["assertions"],
        })
    if len({entry["callable"]["id"] for entry in entries}) != len(entries):
        raise ValueError("verifier manifest callable ids must be unique")
    request = {
        "callable": callable_spec["callable"], "args": callable_spec["args"],
        "kwargs": callable_spec["kwargs"], "assertions": callable_spec["assertions"],
    }
    if request not in entries:
        raise ValueError("PythonCallResultV1 request is not exactly allowlisted")
    return _tracked_safe_path(
        target_root, callable_spec["callable"]["source"], "allowlisted callable source",
    )


def _python_call_request(payload: dict[str, Any]) -> dict[str, Any]:
    callable_spec = payload["callable"]
    return {
        "module": callable_spec["module"],
        "qualname": callable_spec["qualname"],
        "args": payload["args"],
        "kwargs": payload["kwargs"],
        "assertions": payload["assertions"],
    }


def _verify_python_call_result(result: Any, request: Any) -> None:
    if not isinstance(result, dict) or not isinstance(request, dict):
        raise RuntimeError("Python-call verifier evidence is malformed")
    if result.get("format") != "odibi-anchor-python-call-result-v1":
        raise RuntimeError("Python-call verifier result format is invalid")
    state = result.get("state")
    if state == "unavailable" and set(result) == {"format", "state"}:
        return
    if state == "error":
        if set(result) != {"format", "state", "error_type"} or not isinstance(
            result["error_type"], str,
        ):
            raise RuntimeError("Python-call verifier error evidence is malformed")
        return
    if state not in {"passed", "failed"} or set(result) != {"format", "state", "assertions"}:
        raise RuntimeError("Python-call verifier comparison evidence is malformed")
    expected_assertions = request.get("assertions")
    observed_assertions = result["assertions"]
    if not isinstance(expected_assertions, list) or not isinstance(observed_assertions, list):
        raise RuntimeError("Python-call verifier assertions are malformed")
    expected = []
    for assertion in expected_assertions:
        expected.append({
            "path": assertion["path"],
            "matched": True,
            "observed_sha256": hashlib.sha256(
                _json(assertion["equals"]).encode("utf-8"),
            ).hexdigest(),
        })
    all_matched = observed_assertions == expected
    if (state == "passed") != all_matched:
        raise RuntimeError("Python-call verifier state contradicts comparison evidence")
    if state == "failed":
        for item, assertion in zip(observed_assertions, expected_assertions, strict=True):
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "matched", "observed_sha256"}
                or item.get("path") != assertion["path"]
                or not isinstance(item.get("matched"), bool)
                or not isinstance(item.get("observed_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["observed_sha256"])
            ):
                raise RuntimeError("Python-call verifier failed comparison is malformed")


def _scrubbed_subprocess_environment() -> dict[str, str]:
    environment = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def accepted_task_repository(path: str | Path, *, task_window_id: str) -> tuple[Path, str]:
    """Return the exactly verified repository identity bound to an accepted task."""
    from odibi_anchor.codebase._task_authority import (
        _load_verified_record,
        inspect_task_authority,
    )

    inspect_task_authority(path)
    with _connection(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM accepted_task_records WHERE task_window_id=?",
            (task_window_id,),
        ).fetchone()
        if row is None:
            raise ValueError("verifier task has no accepted task authority")
        record = _load_verified_record(row)
    baseline = record.get("repository_baseline")
    if (
        not isinstance(baseline, dict)
        or baseline.get("kind") != "TaskRepositoryBaseline"
        or not isinstance(baseline.get("value"), dict)
    ):
        raise ValueError("verifier task has no repository baseline authority")
    configured_target_ref = baseline["value"].get("configured_target_ref")
    target_root = record.get("identity", {}).get("target_root")
    if not isinstance(target_root, str) or not target_root:
        raise ValueError("verifier task has no target repository authority")
    if not isinstance(configured_target_ref, str) or not configured_target_ref:
        raise ValueError("verifier task has no configured target ref authority")
    return Path(target_root).resolve(), configured_target_ref


def verify_current_source_snapshot(
    path: str | Path, *, task_window_id: str, expected_sha256: str,
) -> dict[str, Any]:
    """Prove current repository bytes still equal an immutable verifier snapshot."""
    target_root, configured_target_ref = accepted_task_repository(
        path, task_window_id=task_window_id,
    )
    actual_sha256, _head_sha, snapshot = _source_snapshot(target_root, configured_target_ref)
    if actual_sha256 != expected_sha256:
        raise ValueError("current repository bytes do not match the tested source snapshot")
    return snapshot


def _contradiction_status(connection: sqlite3.Connection, memory_id: str) -> str:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"memory_selections", "memory_dispositions", "memory_applications", "memory_evaluations"}
    if not required.issubset(tables):
        return "unavailable"
    negative = connection.execute(
        "SELECT count(*) FROM memory_selections s "
        "LEFT JOIN memory_dispositions d ON d.selection_id=s.selection_id "
        "LEFT JOIN memory_applications a ON a.selection_id=s.selection_id "
        "LEFT JOIN memory_evaluations e ON e.application_id=a.application_id "
        "WHERE s.memory_id=? AND (d.disposition='suspect' OR e.outcome IN ('not_helpful','harmful'))",
        (memory_id,),
    ).fetchone()[0]
    return "contradicted" if negative else "clear"


def _attach_safety_withdrawal(
    path: str | Path, response: dict[str, Any], *, memory_id: str, project_id: str,
) -> dict[str, Any]:
    run = response["run"]
    if run["result_state"] == "passed" and run["contradiction_status"] == "clear":
        return response
    with _connection(path, read_only=True) as connection:
        status = connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory_id,),
        ).fetchone()
    if status is not None and status[0] in {"active", "confirmed"}:
        from odibi_anchor.codebase._memory_promotion import withdraw_candidate_activation

        response["safety_withdrawal"] = withdraw_candidate_activation(
            path, memory_id=memory_id, project_id=project_id, quarantine=True,
        )
    return response


def run_installed_verifier(
    path: str | Path, *, memory_id: str, project_id: str, task_window_id: str,
    target_root: str | Path, configured_target_ref: str = "main",
) -> dict[str, Any]:
    """Run a storage-derived closed check and append one immutable result.

    This internal provider boundary accepts no check input, result, claim digest,
    provider identity, or contradiction value from the caller.
    """
    if not all(isinstance(value, str) and value.strip() for value in (
        memory_id, project_id, task_window_id, configured_target_ref,
    )):
        raise ValueError("verifier identities must be non-empty strings")
    initialize_schema(path)
    from odibi_anchor.codebase._task_authority import inspect_task_authority
    from odibi_anchor.codebase._task_execution import inspect_terminal_records

    inspect_task_authority(path)
    with _connection(path) as connection:
        _verify_schema(connection)
        _memory, claim_sha256, source_task, provider, claim_payload = _eligible_claim(
            connection, memory_id=memory_id, project_id=project_id,
        )
        accepted = connection.execute(
            "SELECT project_id,target_root FROM accepted_task_records WHERE task_window_id=?",
            (task_window_id,),
        ).fetchone()
        if accepted is None or accepted["project_id"] != project_id:
            raise ValueError("verifier task is not accepted in the active project")
        if Path(accepted["target_root"]).resolve() != Path(target_root).resolve():
            raise ValueError("verifier target root does not match accepted task authority")
        if source_task == task_window_id:
            raise ValueError("a task cannot verify its own authored memory claim")
        source_records = inspect_terminal_records(
            path, task_window_id=source_task, project_id=project_id,
        )["records"]
        if len(source_records) != 1 or source_records[0]["terminal_status"] != "completed":
            raise ValueError("memory source task has no completed immutable terminal record")
        snapshot_sha256, source_head, snapshot = _source_snapshot(
            target_root, configured_target_ref,
        )
        if source_records[0]["end_revision"] == source_head:
            raise ValueError("verifier evidence must use a source snapshot distinct from claim authorship")
        resolved_root = Path(target_root).resolve()
        contradiction = _contradiction_status(connection, memory_id)
        if provider == PROVIDER:
            selectors = claim_payload["selectors"]
            for selector in selectors:
                selector_path = selector.split("::", 1)[0]
                candidate = resolved_root / selector_path
                if (
                    not candidate.is_file()
                    or candidate.is_symlink()
                    or not candidate.resolve().is_relative_to(resolved_root)
                ):
                    raise ValueError("bound test evidence selector is unavailable or unsafe")
            from odibi_anchor import pytest_runner

            pytest_args = ["--no-header", "-q", "--tb=short", "--color=no", *selectors]
            command = [
                sys.executable, "-B", str(Path(pytest_runner.__file__).resolve()),
                "-p", "no:cacheprovider", *pytest_args,
            ]
            check_input: dict[str, Any] | list[str] = selectors
            check_command_sha256 = _sha256(command)
        else:
            _verified_python_callable(resolved_root, claim_payload)
            from odibi_anchor import behavior_runner

            request = _python_call_request(claim_payload)
            request_json = _json(request)
            command = [sys.executable, "-I", "-B", str(Path(behavior_runner.__file__).resolve())]
            check_input = request
            check_command_sha256 = _sha256({
                "command": command,
                "stdin_sha256": hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
            })
        derivation_id = _sha256(
            {"selectors": check_input, "source_task_window_id": source_task}
            if provider == PROVIDER else {
                "check_input": check_input,
                "source_task_window_id": source_task,
                "verifier_provider": provider,
            }
        )
        evidence_identity = _sha256({
            "claim_sha256": claim_sha256,
            "project_id": project_id,
            "source_snapshot_sha256": snapshot_sha256,
            "check_command_sha256": check_command_sha256,
            "derivation_id": derivation_id,
            "verifier_provider": provider,
            "verifier_version": PROVIDER_VERSION,
        })
        run_id = "mvr_" + _sha256({
            "task_window_id": task_window_id,
            "evidence_identity": evidence_identity,
        })
        existing = connection.execute(
            "SELECT * FROM memory_verifier_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if existing is not None:
            return _attach_safety_withdrawal(
                path, {"kind": "memory_verifier_run", "run": _verified_run(existing)},
                memory_id=memory_id, project_id=project_id,
            )

    started = _now()
    if provider == PROVIDER:
        summary, process = pytest_runner.run_pytest(
            pytest_args, cwd=target_root, timeout=600, capture_output=True,
        )
        state = (
            "unavailable" if process is None or summary["timed_out"]
            else "passed" if process.returncode == 0
            else "failed"
        )
        if process is not None and list(process.args) != command:
            raise RuntimeError("installed verifier command diverged from the bound command")
        result = {
            "state": state,
            "passed": summary["passed"],
            "failed": summary["failed"],
            "errors": summary["errors"],
            "skipped": summary["skipped"],
            "timed_out": summary["timed_out"],
        }
        artifact = {
            "format": "odibi-anchor-pytest-result-v1",
            "selectors": selectors,
            "result": result,
        }
    else:
        try:
            process = subprocess.run(
                command, input=request_json, cwd=resolved_root,
                env=_scrubbed_subprocess_environment(), capture_output=True,
                text=True, timeout=60, check=False,
            )
            if len(process.stdout.encode("utf-8")) > 64_000:
                raise RuntimeError("Python-call verifier output exceeds size limit")
            result = json.loads(process.stdout)
            if process.stdout != _json(result):
                raise RuntimeError("Python-call verifier output is not canonical JSON")
            expected_codes = {"passed": 0, "failed": 1, "error": 2}
            if (
                not isinstance(result, dict)
                or result.get("format") != "odibi-anchor-python-call-result-v1"
                or result.get("state") not in expected_codes
                or process.returncode != expected_codes[result["state"]]
            ):
                raise RuntimeError("Python-call verifier returned an invalid result")
            _verify_python_call_result(result, request)
            state = result["state"]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, UnicodeError):
            result = {
                "format": "odibi-anchor-python-call-result-v1",
                "state": "unavailable",
            }
            state = "unavailable"
        artifact = {
            "format": "odibi-anchor-python-call-artifact-v1",
            "request_sha256": _sha256(request),
            "result": result,
        }
    completed = _now()
    after_sha256, after_head, _after_snapshot = _source_snapshot(
        target_root, configured_target_ref,
    )
    if after_sha256 != snapshot_sha256 or after_head != source_head:
        raise RuntimeError("repository bytes changed during installed verifier execution")
    check_result_sha256 = _sha256(result)
    artifact_sha256 = _sha256(artifact)
    payload = {
        "format": "odibi-anchor-memory-verifier-run-v1",
        "run_id": run_id,
        "memory_id": memory_id,
        "claim_sha256": claim_sha256,
        "project_id": project_id,
        "trust_domain": project_id,
        "task_window_id": task_window_id,
        "source_task_window_id": source_task,
        "selectors": check_input,
        "check_command_sha256": check_command_sha256,
        "check_result_sha256": check_result_sha256,
        "artifact_sha256": artifact_sha256,
        "source_snapshot_sha256": snapshot_sha256,
        "source_head_sha": source_head,
        "source_snapshot": snapshot,
        "verifier_provider": provider,
        "verifier_version": PROVIDER_VERSION,
        "derivation_id": derivation_id,
        "evidence_identity": evidence_identity,
        "result_state": state,
        "contradiction_status": contradiction,
        "result": result,
        "started_at": started,
        "completed_at": completed,
    }
    raw = _json(payload)
    selector_json = _json(check_input)
    with _connection(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO memory_verifier_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, memory_id, claim_sha256, project_id, project_id, task_window_id,
                    source_task, selector_json, check_command_sha256, check_result_sha256,
                    artifact_sha256, snapshot_sha256, source_head, provider, PROVIDER_VERSION,
                    derivation_id, evidence_identity, state, contradiction, raw, started, completed,
                ),
            )
            persisted = connection.execute(
                "SELECT payload_json FROM memory_verifier_runs WHERE run_id=?", (run_id,),
            ).fetchone()
            if persisted is None or persisted[0] != raw:
                raise RuntimeError("conflicting memory verifier run")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _attach_safety_withdrawal(
        path, {"kind": "memory_verifier_run", "run": payload},
        memory_id=memory_id, project_id=project_id,
    )


def _verified_run(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    if row["payload_json"] != _json(payload):
        raise RuntimeError("memory verifier run is not canonical JSON")
    indexed = {
        "run_id": row["run_id"], "memory_id": row["memory_id"],
        "claim_sha256": row["claim_sha256"], "project_id": row["project_id"],
        "trust_domain": row["trust_domain"], "task_window_id": row["task_window_id"],
        "source_task_window_id": row["source_task_window_id"],
        "selectors": json.loads(row["selector_json"]),
        "check_command_sha256": row["check_command_sha256"],
        "check_result_sha256": row["check_result_sha256"],
        "artifact_sha256": row["artifact_sha256"],
        "source_snapshot_sha256": row["source_snapshot_sha256"],
        "source_head_sha": row["source_head_sha"],
        "verifier_provider": row["verifier_provider"],
        "verifier_version": row["verifier_version"],
        "derivation_id": row["derivation_id"],
        "evidence_identity": row["evidence_identity"],
        "result_state": row["result_state"],
        "contradiction_status": row["contradiction_status"],
        "started_at": row["started_at"], "completed_at": row["completed_at"],
    }
    if any(payload.get(key) != value for key, value in indexed.items()):
        raise RuntimeError("memory verifier run indexed column mismatch")
    result = payload.get("result")
    check_input = payload.get("selectors")
    source_snapshot = payload.get("source_snapshot")
    provider = payload.get("verifier_provider")
    if provider == PROVIDER:
        derivation_id = _sha256({
            "selectors": check_input,
            "source_task_window_id": payload.get("source_task_window_id"),
        })
        artifact = {
            "format": "odibi-anchor-pytest-result-v1",
            "selectors": check_input,
            "result": result,
        }
    elif provider == PYTHON_CALL_PROVIDER:
        _verify_python_call_result(result, check_input)
        derivation_id = _sha256({
            "check_input": check_input,
            "source_task_window_id": payload.get("source_task_window_id"),
            "verifier_provider": provider,
        })
        artifact = {
            "format": "odibi-anchor-python-call-artifact-v1",
            "request_sha256": _sha256(check_input),
            "result": result,
        }
    else:
        raise RuntimeError("memory verifier run has an unsupported provider")
    evidence_identity = _sha256({
        "claim_sha256": payload.get("claim_sha256"),
        "project_id": payload.get("project_id"),
        "source_snapshot_sha256": payload.get("source_snapshot_sha256"),
        "check_command_sha256": payload.get("check_command_sha256"),
        "derivation_id": derivation_id,
        "verifier_provider": payload.get("verifier_provider"),
        "verifier_version": payload.get("verifier_version"),
    })
    expected = {
        "format": "odibi-anchor-memory-verifier-run-v1",
        "check_result_sha256": _sha256(result),
        "artifact_sha256": _sha256(artifact),
        "source_snapshot_sha256": _sha256(source_snapshot),
        "source_head_sha": (
            source_snapshot.get("head_sha") if isinstance(source_snapshot, dict) else None
        ),
        "derivation_id": derivation_id,
        "evidence_identity": evidence_identity,
        "run_id": "mvr_" + _sha256({
            "task_window_id": payload.get("task_window_id"),
            "evidence_identity": evidence_identity,
        }),
        "result_state": result.get("state") if isinstance(result, dict) else None,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("memory verifier run derived evidence mismatch")
    return payload


def get_verifier_run(
    path: str | Path, *, run_id: str, project_id: str,
) -> dict[str, Any]:
    """Load one exactly verified run inside its project/trust boundary."""
    with _connection(path, read_only=True) as connection:
        _verify_schema(connection)
        row = connection.execute(
            "SELECT * FROM memory_verifier_runs WHERE run_id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone()
        if row is None:
            raise ValueError("memory verifier run not found in active project")
        return _verified_run(row)


def inspect_verifier_runs(
    path: str | Path, *, project_id: str, memory_id: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Return exactly verified, project-scoped verifier-run diagnostics."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    target = Path(path).expanduser()
    if not target.is_file():
        return {"schema_status": "uninitialized", "counts": {}, "runs": []}
    with _connection(target, read_only=True) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "memory_verifier_runs" not in tables:
            return {"schema_status": "uninitialized", "counts": {}, "runs": []}
        _verify_schema(connection)
        where = ["project_id=?"]
        params: list[Any] = [project_id]
        if memory_id is not None:
            where.append("memory_id=?")
            params.append(memory_id)
        rows = connection.execute(
            "SELECT * FROM memory_verifier_runs WHERE " + " AND ".join(where)
            + " ORDER BY completed_at DESC,run_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        runs = [_verified_run(row) for row in rows]
        counts = dict(connection.execute(
            "SELECT result_state,count(*) FROM memory_verifier_runs WHERE project_id=? "
            "GROUP BY result_state",
            (project_id,),
        ).fetchall())
        return {
            "schema_status": "ready",
            "counts": {"runs": sum(counts.values()), "states": counts},
            "runs": runs,
        }
