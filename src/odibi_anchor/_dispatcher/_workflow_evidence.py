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


def _accepted_task(path, session_state, task_window_id, *, require_open=False):
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
        if require_open:
            tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            frozen = connection.execute(
                "SELECT 1 FROM accepted_task_events WHERE task_window_id=? AND event_type='closed'",
                (task_window_id,),
            ).fetchone()
            for table in ("terminal_task_records", "learning_obligations"):
                if table in tables:
                    frozen = frozen or connection.execute(
                        f"SELECT 1 FROM {table} WHERE task_window_id=?", (task_window_id,),
                    ).fetchone()
            if frozen:
                raise WorkflowError("wrong_authority", "gated or closed tasks cannot produce candidates; replan at a safe boundary")
        return record
    finally:
        connection.close()


def collect_artifact_baseline(plan, *, session_state):
    """Observe planned outputs before implementation, including explicit absence.

    Metadata detects same-byte rewrites; this is a bounded observation, not a
    filesystem lock. Ancillary planning/evidence artifacts are not output paths.
    """
    from odibi_anchor._utils._session_state import is_managed_artifact_path

    if not isinstance(plan, dict) or plan.get("execution_mode") != "artifact_only":
        return None
    root = Path(session_state.artifact_root).resolve()
    files = {}
    for name in _paths(plan, "artifact_paths"):
        item = root / name
        if (any(part.is_symlink() for part in (item, *item.parents) if part.is_relative_to(root))
                or not item.resolve().is_relative_to(root)
                or not is_managed_artifact_path(str(item), artifact_root=str(root),
                                                target_root=session_state.target_root)):
            raise WorkflowError("out_of_scope", "pre-plan artifact requires a managed non-link path")
        try:
            if not item.exists():
                files[name] = None
                continue
            if not item.is_file():
                raise WorkflowError("unavailable", "pre-plan artifact must be a regular file or absent")
            before = item.stat()
            data = item.read_bytes()
            after = item.stat()
            if any(getattr(before, key) != getattr(after, key) for key in (
                "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
            )):
                raise WorkflowError("stale_evidence", "pre-plan artifact changed during observation")
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                           "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns}
        except OSError as exc:
            raise WorkflowError("unavailable", "pre-plan artifact observation unavailable") from exc
    return {"plan_sha256": digest(plan), "files": files}


def check_artifact_baseline(state, *, session_state, path):
    """Never replace unavailable historical evidence with a current snapshot."""
    from odibi_anchor.codebase._workflow_artifact_restore import matches_restored_baseline

    baseline = state.get("artifact_baseline")
    if not baseline or baseline.get("plan_sha256") != state["plan_sha256"]:
        raise WorkflowError("missing_evidence", "pre-plan artifact baseline unavailable; preserve work and recover authority")
    observed = collect_artifact_baseline(state["plan"], session_state=session_state)
    if not matches_restored_baseline(path, state=state, owner=workflow_owner(session_state), observed=observed):
        raise WorkflowError("stale_evidence", "pre-plan artifact changed; preserve work and recover authority")
    return observed


def collect_plan_baseline(path, *, session_state, record):
    """Observe admission without laundering pre-plan edits or adoption."""
    baseline_ref = {"accepted_task_record": record["record_id"]}
    mode = record["task"]["profile"]["execution_mode"]
    if mode == "artifact_only":
        state = read_workflow(path, owner=workflow_owner(session_state),
                              workflow_id=record["task"]["workflow_binding"]["workflow_id"])
        return {**baseline_ref, "artifact_observation": check_artifact_baseline(state, session_state=session_state, path=path)}
    if mode != "source_change":
        return baseline_ref
    from odibi_anchor._repository_snapshot import capture_task_change_scope
    from odibi_anchor.codebase._task_authority import _restore_baseline

    baseline = _restore_baseline(record.get("repository_baseline"), session_state.repository_provider)
    if baseline is None:
        raise WorkflowError("unavailable", "source admission requires accepted repository evidence")
    scope = capture_task_change_scope(baseline, session_state.task_repository_write_fingerprints)
    provenance = scope.provenance
    adoption = getattr(baseline, "adoption_provenance", None)
    if adoption:
        from odibi_anchor.codebase._adopted_dirty import (
            _connect,
            _verify_approval_row,
            adoption_status,
        )

        if adoption_status(path, adoption_id=adoption["adoption_id"])["status"] != "active":
            raise WorkflowError("wrong_authority", "source adoption withdrawn before plan acceptance")
        connection = _connect(path, read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM dirty_adoption_approvals WHERE approval_id=?",
                (adoption["approval_id"],),
            ).fetchone()
            if row is None:
                raise WorkflowError("unavailable", "source adoption approval is unavailable")
            approval = _verify_approval_row(row)
        finally:
            connection.close()
        observed = {key: getattr(scope, key) for key in (
            "branch", "head_sha", "configured_target_ref", "target_sha", "merge_base_sha",
        )}
        observed.update({key: list(getattr(scope, key)) for key in (
            "staged_paths", "unstaged_paths", "untracked_paths", "changed_paths",
        )})
        observed.update({key: provenance[key] for key in ("index_fingerprint", "worktree_fingerprint")})
        if (observed != approval["subject"]["repository"]
                or provenance.get("target_drift") or scope.local_conflict_result != "clear"):
            raise WorkflowError("stale_evidence", "adopted source changed before plan acceptance")
        observation = {"basis": "adopted_pre_plan_source", "adoption_id": adoption["adoption_id"],
                       "approval_id": adoption["approval_id"], "repository": observed}
    else:
        head = getattr(scope, "head_sha", None)
        if (scope.changed_paths or provenance.get("target_drift") or provenance.get("target_birth")
                or head != getattr(baseline, "task_start_head_sha", head)
                or getattr(scope, "local_conflict_result", "clear") != "clear"):
            raise WorkflowError("stale_evidence", "source changed before plan acceptance; preserve work and recover authority")
        observation = {"basis": "unchanged_task_source", "head_sha": head,
                       "evidence_kind": getattr(scope, "evidence_kind", "local_git"),
                       "changed_paths": []}
    return {**baseline_ref, "source_observation": observation}


def producer_completion(path, *, session_state, producer):
    """Verify existing task closure; never synthesize a gate or learning receipt."""
    from odibi_anchor.codebase._task_authority import (
        _canonical,
        _connect,
        _verify_schema,
        verified_task_session,
    )
    from odibi_anchor.codebase._task_execution import inspect_terminal_records

    accepted = _accepted_task(path, session_state, producer)
    identity = accepted["identity"]
    result = inspect_terminal_records(path, task_window_id=producer, project_id=identity["project_id"])
    records = result["records"]
    if result["schema_status"] != "ready" or len(records) != 1:
        raise WorkflowError("missing_evidence", "qualification requires the producer's completed gate-and-learning closure")
    terminal = records[0]
    record = terminal["record"]
    if (record["terminal"]["status"] != "completed"
            or record["identities"]["task_window_id"] != producer
            or record["identities"]["project_id"] != identity["project_id"]
            or not record.get("learning_assessment")):
        raise WorkflowError("missing_evidence", "producer terminal record lacks completed gate-and-learning proof")
    connection = _connect(path, read_only=True)
    try:
        _verify_schema(connection)
        if not verified_task_session(connection, accepted=accepted,
                                     session_id=record["identities"]["session_id"], ended_at=record["ended_at"]):
            raise WorkflowError("missing_evidence", "producer terminal record lacks verified task-session lineage")
        closure = connection.execute(
            "SELECT event_id,event_json,event_sha256,created_at FROM accepted_task_events "
            "WHERE task_window_id=? AND event_type='closed'", (producer,),
        ).fetchone()
        if closure is None:
            raise WorkflowError("missing_evidence", "producer terminal record has no committed task closure")
        event = json.loads(closure["event_json"])
        if (_canonical(event) != closure["event_json"]
                or hashlib.sha256(closure["event_json"].encode()).hexdigest() != closure["event_sha256"]
                or event != {"event_id": closure["event_id"], "task_window_id": producer,
                             "event_type": "closed", "details": {"terminal_status": "completed"},
                             "created_at": closure["created_at"]}):
            raise WorkflowError("integrity", "producer completed closure integrity check failed")
    finally:
        connection.close()
    return terminal["record_sha256"]


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
    from odibi_anchor.planning._task_profile import TaskProfile

    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    producer = (state.get("candidate") or {}).get("producer") or session_state.task_window_id
    record = _accepted_task(path, session_state, producer)
    binding = record["task"].get("workflow_binding") or {}
    if (binding.get("workflow_id") != workflow_id
            or binding.get("plan_sha256") != state["plan_sha256"]):
        raise WorkflowError("stale_plan", "producing task is not bound to this exact plan")
    plan = state["plan"]
    validate_producer_policy(plan, TaskProfile.from_dict(record["task"]["profile"]))
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
        from odibi_anchor.codebase._workflow_artifact_restore import matches_restored_baseline

        baseline = state.get("artifact_baseline")
        admission = state.get("admission") or {}
        admitted = admission.get("baseline", {}).get("artifact_observation")
        if (not baseline or baseline.get("plan_sha256") != state["plan_sha256"]
                or admission.get("authority_ref") != "accepted_task:" + producer
                or admission.get("baseline", {}).get("accepted_task_record") != record["record_id"]
                or not matches_restored_baseline(path, state=state, owner=workflow_owner(session_state),
                                                observed=admitted, historical=True)):
            raise WorkflowError("missing_evidence", "candidate lacks exact pre-plan artifact admission evidence")
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


def validate_producer_policy(plan: dict[str, Any], profile: Any) -> None:
    """Enforce declared producer risk and supported checks, not semantic certainty."""
    from odibi_anchor.assurance.evaluator import build_assurance_plan

    ranks = {"low": 0, "medium": 1, "high": 2}
    floor = "high" if build_assurance_plan(profile).tier == "T3" else profile.risk
    if ranks[plan["risk"]] < ranks[floor]:
        raise WorkflowError("risk_downgrade", "workflow plan cannot downgrade accepted producer risk")
    artifact_checks = set()
    has_pytest = False
    for criterion in plan.get("criteria", []):
        method = criterion.get("method")
        if method == "pytest":
            targets = criterion.get("test_targets")
            if (not isinstance(targets, list) or not targets
                    or any(not isinstance(t, str) or not t.strip() for t in targets)):
                raise WorkflowError("missing_evidence", "pytest criterion requires explicit test_targets")
            has_pytest = True
        elif method == "artifact_sha256" and plan["execution_mode"] == "artifact_only":
            expected = criterion.get("expected_sha256")
            if (not isinstance(expected, dict) or not expected
                    or set(expected) - set(_paths(plan, "artifact_paths"))
                    or any(not isinstance(value, str) or len(value) != 64
                           or any(char not in "0123456789abcdef" for char in value)
                           for value in expected.values())):
                raise WorkflowError("missing_evidence", "artifact criterion requires exact planned paths and SHA256 digests")
            artifact_checks.update(expected)
        else:
            raise WorkflowError("unavailable", "criterion method has no supported trusted collector")
    if artifact_checks and not has_pytest and artifact_checks != set(_paths(plan, "artifact_paths")):
        raise WorkflowError("missing_evidence", "artifact checks must cover all planned artifact paths")


def collect_artifact_measurement(path: str | Path, *, session_state: Any,
                                 workflow_id: str, criterion_id: str) -> dict[str, Any]:
    """Compare observed bytes with independently planned digests, without pytest."""
    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    criteria = [c for c in state["plan"].get("criteria", []) if c["id"] == criterion_id]
    if (state["progress"] != "implemented" or state["status"] != "active"
            or len(criteria) != 1 or criteria[0]["method"] != "artifact_sha256"):
        raise WorkflowError("missing_evidence", "artifact measurement requires an implemented artifact criterion")
    before = collect_candidate(path, session_state=session_state, workflow_id=workflow_id)
    if before != state["candidate"] or before["kind"] != "managed_artifacts":
        raise WorkflowError("stale_evidence", "artifact candidate changed before measurement")
    expected = criteria[0]["expected_sha256"]
    observed = {name: before["snapshot"][name]["sha256"] for name in expected}
    if collect_candidate(path, session_state=session_state, workflow_id=workflow_id) != before:
        raise WorkflowError("stale_evidence", "artifact candidate changed across measurement")
    result = {"expected_sha256": expected, "observed_sha256": observed}
    return {"plan_sha256": state["plan_sha256"], "candidate_sha256": digest(before),
            "criterion_id": criterion_id, "method": "artifact_sha256",
            "collector": "anchor.managed_artifact_sha256",
            "status": "satisfied" if observed == expected else "failed",
            "evidence_ref": digest(result), "result": result}


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
                     expected_generation: int, request_id: str,
                     public_request: dict[str, Any] | None = None) -> dict[str, Any]:
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
           for check in state.get("measurements", {}).values() if check["method"] == "pytest"):
        raise WorkflowError("stale_evidence", "qualification environment differs from retained measurement")
    completion = producer_completion(path, session_state=session_state, producer=candidate["producer"])
    return transition_workflow(
        path, owner=owner, workflow_id=workflow_id, expected_generation=expected_generation,
        request_id=request_id, operation="qualify", payload={
            "plan_sha256": state["plan_sha256"], "candidate_sha256": digest(candidate),
            "checks": list(state.get("measurements", {}).values()), "review": review,
            "producer_terminal_record_sha256": completion,
        }, public_request=public_request,
    )
