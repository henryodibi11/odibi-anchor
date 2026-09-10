"""Canonical planning-to-implementation handoff derived from committed authority."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, cast


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in fields(value)
            if field.name != "identity_provider"
        }
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    return str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _managed_records(session_state: Any) -> dict[str, Any]:
    root = Path(session_state.artifact_root)
    result: dict[str, Any] = {
        "problem": None,
        "spec": None,
        "work_item": None,
        "decisions": [],
        "content_sha256": {},
    }

    def record_content(kind: str, path: str | os.PathLike[str] | None) -> None:
        if not path:
            return
        candidate = Path(path)
        result["content_sha256"][kind] = {
            "path": candidate.relative_to(root).as_posix(),
            "sha256": "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }

    if session_state.linked_problem:
        from odibi_anchor._dispatcher._problem import problem_action

        problem_record = cast(
            dict[str, Any],
            problem_action(
                root, "resume", session_state.linked_problem,
                project_id=session_state.active_project, output_format="dict",
            ),
        )
        result["problem"] = problem_record
        record_content("problem", problem_record.get("artifact_path"))
    if session_state.linked_spec:
        from odibi_anchor.codebase._spec_parser import find_spec

        spec_record = find_spec(root / "specs", session_state.linked_spec)
        result["spec"] = spec_record
        record_content("spec", (spec_record or {}).get("raw_path"))
    if session_state.linked_work_item:
        from odibi_anchor._dispatcher._work_item import work_item_action

        shown = cast(
            dict[str, Any],
            work_item_action(
                root, "show", session_state.linked_work_item, output_format="dict",
            ),
        )
        result["work_item"] = shown.get("record")
        record_content("work_item", shown.get("artifact_path"))
    decisions = root / "decisions"
    if decisions.is_dir():
        for path in sorted(decisions.glob("*.md"))[:12]:
            content = path.read_text(encoding="utf-8")
            result["decisions"].append({
                "path": path.relative_to(root).as_posix(),
                "sha256": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
                "summary": content[:2_000],
            })
    return result


def _repository_state(session_state: Any) -> dict[str, Any]:
    baseline = session_state.task_repository_baseline
    if baseline is None:
        return {"status": "not_applicable", "reason": "accepted task has no repository baseline"}
    from odibi_anchor._dispatcher._baseline_qualification import (
        baseline_fingerprint,
        project_baseline_qualification,
    )
    from odibi_anchor._repository_snapshot import (
        _source_state_fingerprint,
        capture_task_change_scope,
        databricks_task_baseline_projection,
        is_databricks_git_folder_baseline,
    )

    scope = capture_task_change_scope(
        baseline, getattr(session_state, "task_repository_write_fingerprints", {}),
    )
    if is_databricks_git_folder_baseline(baseline):
        baseline_projection = databricks_task_baseline_projection(baseline)
        current_fingerprint = _digest({
            "identity": _json_safe(getattr(scope, "identity", None)),
            "content_changes": scope.provenance.get("content_changes", {}),
        })
    else:
        baseline_projection = _json_safe(baseline)
        current_fingerprint = "sha256:" + _source_state_fingerprint(
            Path(scope.target_worktree), tuple(scope.changed_paths),
        )
    return {
        "status": "verified",
        "baseline_sha256": baseline_fingerprint(baseline),
        "baseline": baseline_projection,
        "qualification": project_baseline_qualification(
            getattr(session_state, "task_repository_baseline_qualification", None),
            baseline,
            task_window_id=session_state.task_window_id,
        ),
        "current": {
            "target_worktree": scope.target_worktree,
            "branch": getattr(scope, "branch", None),
            "head_sha": getattr(scope, "head_sha", None),
            "changed_paths": list(scope.changed_paths),
            "target_drift": scope.provenance.get("target_drift"),
            "index_fingerprint": scope.provenance.get("index_fingerprint"),
            "content_fingerprint": current_fingerprint,
        },
    }


def _first_action(
    session_state: Any,
    route_binding: Any,
    task_context: Mapping[str, Any],
    work_item: Mapping[str, Any] | None,
    baseline: Any,
    repository: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a complete implementation-task invocation for a fresh receiver."""
    intent = task_context.get("intent") or {}
    background = task_context.get("background") or {}
    scope = task_context.get("scope") or {}
    verification = task_context.get("verification") or {}
    resources = task_context.get("resources") or {}
    profile = session_state.active_task_profile
    kwargs: dict[str, Any] = {
        "goal": intent.get("goal") or session_state.task_goal,
        "mode": "implementation",
        "work_type": "change",
        "execution_mode": "source_change",
        "risk": profile.risk,
        "rigor": profile.rigor,
        "domains": list(profile.domains),
        "traits": sorted(profile.traits),
        "caller_required_evidence": [
            {
                "id": item.id,
                "kind": item.kind,
                "description": item.description,
                "required_before": item.required_before,
            }
            for item in profile.caller_required_evidence
        ],
        "acceptance_criteria": (
            verification.get("acceptance_criteria")
            or (work_item or {}).get("acceptance_criteria")
            or []
        ),
    }
    optional = {
        "problem": session_state.linked_problem,
        "spec": session_state.linked_spec,
        "work_item": session_state.linked_work_item,
        "in_scope": scope.get("in_scope"),
        "out_of_scope": scope.get("out_of_scope"),
        "constraints": task_context.get("constraints"),
        "background": background.get("summary"),
        "current_state": background.get("current_state"),
        "desired_outcome": intent.get("desired_outcome"),
        "trigger": background.get("trigger"),
        "artifacts": resources.get("artifacts"),
        "inputs": resources.get("inputs"),
        "dependencies": resources.get("dependencies"),
        "risks": task_context.get("risks"),
        "stop_conditions": verification.get("stop_conditions"),
        "deliverables": verification.get("deliverables"),
        "expected_output_format": verification.get("expected_output_format"),
        "guardrails": task_context.get("guardrails"),
    }
    kwargs.update({key: value for key, value in optional.items() if value})
    from odibi_anchor._repository_snapshot import DatabricksGitFolderTaskBaseline

    if isinstance(baseline, DatabricksGitFolderTaskBaseline):
        kwargs["repository_scope"] = [item or "." for item in baseline.repository_scope]
        kwargs["accept_unknown_git_state"] = True
    task = intent.get("task") or session_state.task_goal or "Continue the accepted implementation"
    changed_paths = (repository.get("current") or {}).get("changed_paths") or []
    if profile.execution_mode == "source_change" and changed_paths:
        project = route_binding.project_id
        anchor_home = route_binding.anchor_home
        expected_task = session_state.task_window_id
        copy_ready = (
            "from odibi_anchor._dispatcher._project import resolve_route_binding as _rrb\n"
            "from odibi_anchor.bootstrap import init as _cw_init\n"
            "from odibi_anchor._utils._session_state import _SESSION_STATE as _receiver_state\n"
            "import uuid as _uuid\n"
            "_repository_provider = _receiver_state.repository_provider\n"
            f"_route = _rrb({anchor_home!r}, project={project!r}, "
            "runtime_instance_id='handoff:' + _uuid.uuid4().hex)\n"
            "anchor, ROOT, MANIFEST = _cw_init(route_binding=_route, "
            "repository_provider=_repository_provider, rebind_task=True, output_format='dict')\n"
            f"assert anchor._task_rebind_result['task_window_id'] == {expected_task!r}"
        )
        return {
            "action": "bootstrap_rebind",
            "kind": "resume_existing_implementation_task",
            "args": [],
            "kwargs": {"project_id": project, "task_window_id": expected_task},
            "copy_ready": copy_ready,
        }
    if (
        getattr(session_state, "repository_provider", None) is not None
        and not isinstance(baseline, DatabricksGitFolderTaskBaseline)
    ):
        inputs = {"task": task, **kwargs}
        return {
            "action": "prepare",
            "kind": "prepare_implementation_task",
            "args": [],
            "kwargs": {"operation": "task.create", "inputs": inputs},
            "copy_ready": (
                "anchor(\"prepare\", operation='task.create', inputs=" + repr(inputs) + ")"
            ),
        }
    parts = [json.dumps("task"), repr(task)]
    parts.extend(f"{key}={value!r}" for key, value in sorted(kwargs.items()))
    return {
        "action": "task",
        "kind": "create_implementation_task",
        "args": [task],
        "kwargs": kwargs,
        "copy_ready": "anchor(" + ", ".join(parts) + ")",
    }


def _persist(root: Path, task_window_id: str, packet: Mapping[str, Any]) -> tuple[Path, str]:
    payload = json.dumps(_json_safe(packet), sort_keys=True, indent=2) + "\n"
    digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    from odibi_anchor.operational._artifacts import _is_link_or_reparse, _safe_directory

    if _is_link_or_reparse(root):
        raise ValueError("artifact root must not be a link or reparse point")
    root = root.resolve(strict=True)
    directory = _safe_directory(root, Path("notebooks") / "handoffs")
    destination = directory / f"{task_window_id}-{digest[7:19]}.json"
    if destination.is_symlink():
        raise ValueError("handoff destination must not be a symlink or reparse point")
    descriptor, temporary = tempfile.mkstemp(prefix=".handoff-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination, digest


def canonical_handoff(
    session_state: Any,
    route_binding: Any,
    *,
    summary: str | None = None,
    state: str = "in_progress",
    decisions: Sequence[str] | None = None,
    rejected_alternatives: Sequence[str] | None = None,
    blockers: Sequence[str] | None = None,
    evidence_chain: Sequence[Mapping[str, Any]] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    context_needed: Sequence[str] | None = None,
    skip: Sequence[str] | None = None,
    subject: str | None = None,
    open_questions: Sequence[str] | None = None,
    next_steps: Sequence[str] | None = None,
    output_format: str = "dict",
    persist: bool = True,
    **unknown: Any,
) -> dict[str, Any] | str:
    """Build and optionally persist one self-contained implementation handoff."""
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be 'dict' or 'markdown'")
    if unknown:
        raise ValueError("unsupported canonical handoff fields: " + ", ".join(sorted(unknown)))
    if route_binding is None or not getattr(session_state, "task_window_id", None):
        raise RuntimeError("canonical handoff requires exact RouteBinding and accepted task authority")
    task_context = _json_safe(getattr(session_state, "task_handoff_context", {}) or {})
    records = _managed_records(session_state)
    work_item = records["work_item"]
    repository = _repository_state(session_state)
    from odibi_anchor._dispatcher._runtime_capabilities import collect_runtime_capabilities

    authority = {
        "project_id": route_binding.project_id,
        "target_root": route_binding.target_root,
        "artifact_root": route_binding.artifact_root,
        "route_fingerprint": _digest({
            "project_id": route_binding.project_id,
            "target_root": route_binding.target_root,
            "artifact_root": route_binding.artifact_root,
            "anchor_home": route_binding.anchor_home,
        }),
        "task_window_id": session_state.task_window_id,
        "problem_id": session_state.linked_problem,
        "spec_name": session_state.linked_spec,
        "work_item_id": session_state.linked_work_item,
        "repository_baseline_sha256": repository.get("baseline_sha256"),
        "repository_state_sha256": _digest(repository),
        "managed_record_sha256": _digest(records),
    }
    first_action = _first_action(
        session_state, route_binding, task_context, work_item,
        session_state.task_repository_baseline, repository,
    )
    background = task_context.get("background") or {}
    business_reason = background.get("summary")
    missing_semantics = [] if business_reason else [
        {"field": "business_reason", "status": "missing", "reason": "not supplied by accepted task or managed authority"}
    ]
    from odibi_anchor.planning.handoff_context import handoff_context

    packet = cast(dict[str, Any], handoff_context(
        summary or session_state.task_goal or "implementation handoff",
        state=state,
        goal=(task_context.get("intent") or {}).get("goal") or session_state.task_goal,
        decisions=list(decisions or ()),
        rejected_alternatives=list(rejected_alternatives or ()),
        blockers=list(blockers or ()),
        evidence_chain=[dict(item) for item in (evidence_chain or ())],
        artifacts=[dict(item) for item in (artifacts or ())],
        context_needed=list(context_needed or ()),
        skip=list(skip or ()),
        subject=subject,
        open_questions=list(open_questions or ()),
        next_action=first_action["copy_ready"],
        output_format="dict",
    ))
    packet.update({
        "schema_version": "2.0",
        "authority": authority,
        "authority_sha256": _digest(authority),
        "origin_runtime": {
            "runtime_instance_id": route_binding.runtime_instance_id,
            "status": "attested",
            "provenance": {"source": "originating_route_binding:v1"},
        },
        "objective": (task_context.get("intent") or {}).get("goal") or session_state.task_goal,
        "business_reason": business_reason,
        "missing_semantics": missing_semantics,
        "scope": task_context.get("scope") or {
            "in_scope": [work_item.get("scope")] if work_item and work_item.get("scope") else [],
            "out_of_scope": [work_item.get("non_goals")] if work_item and work_item.get("non_goals") else [],
        },
        "source": repository,
        "managed_authority": records,
        "acceptance_criteria": (
            (task_context.get("verification") or {}).get("acceptance_criteria")
            or (work_item or {}).get("acceptance_criteria") or []
        ),
        "required_validation": (
            (work_item or {}).get("validation_notes")
            or (task_context.get("verification") or {}).get("verification_steps") or []
        ),
        "constraints": task_context.get("constraints") or [],
        "policy": {
            "task_profile": session_state.active_task_profile.to_dict(),
            "guardrails": task_context.get("guardrails") or [],
            "assurance_plan": _json_safe(
                getattr(session_state, "active_assurance_plan", None)
            ),
        },
        "risks_and_stop_conditions": {
            "risks": task_context.get("risks") or [],
            "stop_conditions": (task_context.get("verification") or {}).get("stop_conditions") or [],
            "open_questions": list(open_questions or ()),
        },
        "evidence": [_json_safe(item) for item in getattr(session_state, "evidence_ledger", ())],
        "expected_artifacts": [_json_safe(item) for item in getattr(session_state, "managed_artifact_ledger", ())],
        "capabilities": collect_runtime_capabilities(session_state, route_binding),
        "definition_of_done": (
            (task_context.get("verification") or {}).get("acceptance_criteria")
            or (work_item or {}).get("acceptance_criteria") or []
        ),
        "execution_recommendation": {"thread": "fresh", "environment": "same"},
        "first_action": first_action,
        "write_performed": persist,
    })
    if next_steps:
        packet["continuation"]["remaining_steps"] = list(next_steps)
    if persist:
        path, content_digest = _persist(
            Path(route_binding.artifact_root), session_state.task_window_id, packet,
        )
        packet["artifact_path"] = str(path)
        packet["content_sha256"] = content_digest
    if output_format == "dict":
        return packet
    from odibi_anchor.planning.handoff_context import render_handoff_report

    return render_handoff_report(packet) + (
        f"\n## Canonical authority\n\nDigest: `{packet['authority_sha256']}`\n"
        f"First action: `{first_action['copy_ready']}`\n"
    )


def validate_canonical_handoff(
    packet: Mapping[str, Any], session_state: Any, route_binding: Any,
) -> dict[str, Any]:
    """Re-derive authority and report whether a persisted handoff remains executable."""
    expected = packet.get("authority")
    if not isinstance(expected, Mapping):
        return {
            "status": "unverified",
            "reason": "handoff has no canonical authority projection",
            "conflicts": ["authority"],
        }
    current = cast(
        dict[str, Any], canonical_handoff(session_state, route_binding, persist=False)
    )
    actual = current["authority"]
    conflicts = sorted(
        key for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    )
    if packet.get("first_action") != current.get("first_action"):
        conflicts.append("first_action")
        conflicts.sort()
    return {
        "status": "stale" if conflicts else "verified",
        "reason": (
            "current route, managed records, or repository state differs from the handoff"
            if conflicts else "canonical authority matches current verified state"
        ),
        "conflicts": conflicts,
        "expected_authority_sha256": packet.get("authority_sha256"),
        "current_authority_sha256": current["authority_sha256"],
        "first_action": None if conflicts else packet.get("first_action"),
    }
