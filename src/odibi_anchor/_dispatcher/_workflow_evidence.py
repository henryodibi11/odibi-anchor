"""Runtime-derived workflow candidates and review provenance.

These collectors are internal. They do not authenticate an agent principal,
perform external effects, or treat caller-authored findings as measurements.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from odibi_anchor._dispatcher._workflow_admission import workflow_owner
from odibi_anchor.codebase._workflow import WorkflowError, canonical, digest, read_workflow


def _accepted_task(path, session_state, task_window_id):
    from odibi_anchor.codebase._authority_relocation import load_relocations
    from odibi_anchor.codebase._task_authority import (
        _connect,
        _load_verified_record,
        _matches_owner,
        _owner_identity,
        _verify_schema,
    )

    connection = _connect(path, read_only=True)
    try:
        _verify_schema(connection)
        row = connection.execute(
            "SELECT * FROM accepted_task_records WHERE task_window_id=?", (task_window_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError("unavailable", "accepted workflow task is unavailable")
        record = _load_verified_record(row)
        if not _matches_owner(record, _owner_identity(session_state), load_relocations(connection)):
            raise WorkflowError("wrong_authority", "workflow task belongs to another exact authority")
        return record
    finally:
        connection.close()


def _paths(plan, key):
    paths = plan.get(key)
    if not isinstance(paths, list) or not paths:
        raise WorkflowError("scope_required", f"plan requires unique exact {key}")
    for item in paths:
        if (not isinstance(item, str) or not item or "\\" in item
                or any(char in item for char in "*?[") or PurePosixPath(item).is_absolute()
                or ".." in PurePosixPath(item).parts or PurePosixPath(item).as_posix() != item
                or item == "."):
            raise WorkflowError("scope_required", f"{key} must contain canonical relative files")
    if len(paths) != len(set(paths)):
        raise WorkflowError("scope_required", f"plan requires unique exact {key}")
    return paths


def collect_candidate(path: str | Path, *, session_state: Any,
                      workflow_id: str) -> dict[str, Any]:
    """Read exact current bytes under the persisted producing task's authority."""
    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    producer = (state.get("candidate") or {}).get("producer") or session_state.task_window_id
    record = _accepted_task(path, session_state, producer)
    binding = record["task"].get("workflow_binding") or {}
    if (binding.get("workflow_id") != workflow_id
            or binding.get("plan_sha256") != state["plan_sha256"]):
        raise WorkflowError("stale_plan", "producing task is not bound to this exact plan")
    plan = state["plan"]
    mode = plan["execution_mode"]
    if record["task"]["profile"]["execution_mode"] != mode:
        raise WorkflowError("wrong_mode", "candidate producer execution mode differs from plan")
    if mode == "source_change":
        from odibi_anchor._repository_snapshot import capture_task_change_scope
        from odibi_anchor.codebase._task_authority import _restore_baseline

        baseline = _restore_baseline(record.get("repository_baseline"), session_state.repository_provider)
        if baseline is None:
            raise WorkflowError("unavailable", "source candidate requires accepted repository evidence")
        acknowledgements = session_state.task_repository_write_fingerprints
        prior = state.get("candidate") or {}
        if producer != session_state.task_window_id and prior.get("kind") == "databricks_git_folder":
            acknowledgements = {
                name: "absent" if entry["final_sha256"] is None
                else f"file:{entry['final_sha256']}:{entry['final_size']}"
                for name, entry in prior["snapshot"]["content_changes"].items()
            }
        scope = capture_task_change_scope(baseline, acknowledgements)
        allowed = _paths(plan, "source_paths")
        if set(scope.changed_paths) - set(allowed):
            raise WorkflowError("out_of_scope", "observed source changes exceed the accepted plan")
        root = Path(session_state.target_root).resolve()
        for name in scope.changed_paths:
            if (root / name).is_symlink() or not (root / name).resolve().is_relative_to(root):
                raise WorkflowError("out_of_scope", "source candidate contains an unsupported link")
        provenance = scope.provenance
        if getattr(scope, "evidence_kind", None) == "databricks_git_folder":
            kind = "databricks_git_folder"
            snapshot = {"repository_id": scope.identity.repository_id,
                        "workspace_path": scope.identity.workspace_path,
                        "head_sha": scope.identity.head_sha, "branch": scope.identity.branch,
                        "content_changes": {key: dict(value) for key, value in provenance["content_changes"].items()}}
        else:
            if getattr(scope, "local_conflict_result", "unknown") != "clear":
                raise WorkflowError("unavailable", "candidate requires verified conflict-free Git state")
            kind = "git"
            snapshot = {"head_sha": scope.head_sha, "branch": scope.branch,
                        "target_sha": scope.target_sha,
                        "index_fingerprint": provenance["index_fingerprint"],
                        "worktree_fingerprint": provenance["worktree_fingerprint"],
                        "changed_paths": list(scope.changed_paths)}
    elif mode == "artifact_only":
        from odibi_anchor._utils._session_state import is_managed_artifact_path

        kind = "managed_artifacts"
        root = Path(session_state.artifact_root).resolve()
        snapshot = {}
        for name in _paths(plan, "artifact_paths"):
            item = root / name
            if (not item.is_file() or item.is_symlink() or not item.resolve().is_relative_to(root)
                    or not is_managed_artifact_path(str(item), artifact_root=str(root),
                                                    target_root=session_state.target_root)):
                raise WorkflowError("unavailable", "candidate requires existing managed regular artifacts")
            data = item.read_bytes()
            snapshot[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    else:
        raise WorkflowError("unavailable", "this candidate kind has no supported content collector")
    return {"kind": kind, "identity": digest(snapshot), "producer": producer,
            "snapshot": snapshot}


def bind_review(path: str | Path, *, session_state: Any, workflow_id: str) -> dict[str, Any]:
    """Prepare exact review subject for persistence with a read-only task."""
    owner = workflow_owner(session_state)
    state = read_workflow(path, owner=owner, workflow_id=workflow_id)
    profile = session_state.active_task_profile
    if profile is None or profile.execution_mode != "read_only" or profile.work_type != "verify":
        raise WorkflowError("review_required", "review binding requires read-only verification task")
    if state["progress"] != "implemented" or state["status"] != "active":
        raise WorkflowError("missing_evidence", "review binding requires an active implemented candidate")
    return {"schema_version": 1, "workflow_id": workflow_id,
            "task_window_id": session_state.task_window_id, "owner_sha256": digest(state["owner"]),
            "plan_sha256": state["plan_sha256"], "execution_mode": "read_only",
            "review_candidate_sha256": digest(state["candidate"])}


def collect_review(path: str | Path, *, session_state: Any, workflow_id: str,
                   findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind critique to runtime task separation without claiming authentication."""
    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    if state["progress"] != "implemented" or state["candidate"] is None:
        raise WorkflowError("missing_evidence", "review requires an implemented candidate")
    candidate = collect_candidate(path, session_state=session_state, workflow_id=workflow_id)
    if candidate != state["candidate"]:
        raise WorkflowError("stale_evidence", "candidate changed before review collection")
    reviewer = _accepted_task(path, session_state, session_state.task_window_id)
    producer = _accepted_task(path, session_state, candidate["producer"])
    distinct = session_state.task_window_id != candidate["producer"]
    profile = reviewer["task"]["profile"]
    if distinct and (profile["execution_mode"] != "read_only" or profile["work_type"] != "verify"):
        raise WorkflowError("review_required", "independent critique requires an accepted read-only verification task")
    if state["plan"]["risk"] == "high" and not distinct:
        raise WorkflowError("review_required", "high-risk work cannot review its own candidate")
    if distinct:
        expected = bind_review(path, session_state=session_state, workflow_id=workflow_id)
        if reviewer["task"].get("workflow_binding") != expected:
            raise WorkflowError("stale_evidence", "review task is not bound to this exact plan and candidate")
    if not isinstance(findings, list):
        raise ValueError("review findings must be an explicit array")
    findings = json.loads(canonical(findings))
    for finding in findings:
        if (not isinstance(finding, dict) or not isinstance(finding.get("summary"), str)
                or not finding["summary"].strip() or finding.get("status") not in {"resolved", "open"}):
            raise ValueError("each review finding requires summary and open/resolved status")
    return {"plan_sha256": state["plan_sha256"], "candidate_sha256": digest(candidate),
            "kind": "independent" if distinct else "self", "reviewer": session_state.task_window_id,
            "status": "failed" if any(f["status"] == "open" for f in findings) else "satisfied",
            "findings": findings, "evidence_ref": digest(findings),
            "independence": "separate_read_only_accepted_task" if distinct else "self_verification",
            "separate_read_only_accepted_task": distinct,
            "reviewer_authentication": "none", "evidence_kind": "agent_judgment",
            "distinct_session": reviewer["identity"]["session_id"] != producer["identity"]["session_id"]}


def runtime_environment() -> dict[str, Any]:
    """Observe interpreter/distribution metadata, not unobservable host history."""
    return {"python": sys.version, "executable": str(Path(sys.executable).resolve()),
            "distributions": sorted(
                [[d.metadata["Name"], d.version] for d in importlib.metadata.distributions()],
                key=lambda item: (item[0] or "", item[1]),
            )}


def collect_test_measurement(*, state: dict[str, Any], before: dict[str, Any],
                             after: dict[str, Any], criterion_id: str, targets: list[str],
                             environment_before: dict[str, Any],
                             result: dict[str, Any]) -> dict[str, Any]:
    """Convert a trusted runner result, never a transport-provided pass claim."""
    from odibi_anchor._forensic_replay.journal import redact_payload

    if state["candidate"] != before or before != after:
        raise WorkflowError("stale_evidence", "candidate changed across the measurement")
    criteria = [c for c in state["plan"]["criteria"] if c["id"] == criterion_id]
    if len(criteria) != 1 or criteria[0]["method"] != "pytest":
        raise WorkflowError("missing_evidence", "measurement must match an exact pytest criterion")
    if not targets or criteria[0].get("test_targets") != targets:
        raise WorkflowError("missing_evidence", "executed test targets differ from the accepted criterion")
    environment = runtime_environment()
    if environment_before != environment:
        raise WorkflowError("stale_evidence", "qualification environment changed during measurement")
    metrics = result.get("metrics")
    if result.get("kind") != "test_run" or not isinstance(metrics, dict):
        raise WorkflowError("unavailable", "trusted pytest measurement is unavailable")
    counts = {name: metrics.get(name) for name in ("passed", "failed", "errors", "skipped")}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise WorkflowError("unavailable", "test measurement lacks complete exact counts")
    passed = (type(metrics.get("exit_code")) is int and metrics["exit_code"] == 0
              and metrics.get("timed_out") is False and counts["passed"] > 0
              and counts["failed"] == counts["errors"] == counts["skipped"] == 0)
    retained = json.loads(canonical(redact_payload(result)))
    return {"plan_sha256": state["plan_sha256"], "candidate_sha256": digest(before),
            "criterion_id": criterion_id, "method": "pytest", "collector": "anchor.pytest_runner",
            "status": "satisfied" if passed else "failed", "counts": counts,
            "targets": targets, "environment": environment, "environment_sha256": digest(environment),
            "evidence_ref": digest(retained), "result": retained}


def qualify_recorded(path: str | Path, *, session_state: Any, workflow_id: str,
                     expected_generation: int, request_id: str) -> dict[str, Any]:
    """Qualify only retained collector evidence after a fresh candidate read."""
    from odibi_anchor.codebase._workflow import transition_workflow

    owner = workflow_owner(session_state)
    state = read_workflow(path, owner=owner, workflow_id=workflow_id)
    _accepted_task(path, session_state, session_state.task_window_id)
    candidate = collect_candidate(path, session_state=session_state, workflow_id=workflow_id)
    if candidate != state["candidate"]:
        raise WorkflowError("stale_evidence", "candidate changed since implementation")
    review = state.get("review_result")
    if not review:
        raise WorkflowError("review_required", "no retained review evidence")
    environment_sha256 = digest(runtime_environment())
    if any(check.get("environment_sha256") != environment_sha256
           for check in state.get("measurements", {}).values()):
        raise WorkflowError("stale_evidence", "qualification environment differs from retained measurement")
    return transition_workflow(
        path, owner=owner, workflow_id=workflow_id, expected_generation=expected_generation,
        request_id=request_id, operation="qualify", payload={
            "plan_sha256": state["plan_sha256"], "candidate_sha256": digest(candidate),
            "checks": list(state.get("measurements", {}).values()), "review": review,
        },
    )
