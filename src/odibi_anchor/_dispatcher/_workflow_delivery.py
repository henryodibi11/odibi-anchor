"""Internal human delivery authority and read-only destination observations.

No function here pushes, merges, publishes, uploads or deploys. A transport may
expose preparation/observation, never caller-authored grants or reader callbacks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import quote

from odibi_anchor._dispatcher._workflow_admission import workflow_owner
from odibi_anchor._dispatcher._workflow_evidence import collect_candidate, producer_completion, runtime_environment
from odibi_anchor.codebase._workflow import (
    WorkflowError,
    canonical,
    digest,
    read_workflow,
    reconciliation_evidence,
    transition_workflow,
)

_OPERATIONS = {"managed_artifacts": "deliver_artifacts", "github_ref": "push",
               "github_release": "publish_release", "pypi_release": "publish_package",
               "databricks_workspace_files": "upload_workspace_files",
               "local_git_ref": "update_local_ref"}
# Exact keys per destination kind. local_git_ref is a branch or tag of the target
# repository itself, for projects that have no remote.
DESTINATION_KEYS = {"managed_artifacts": {"kind"},
                    "github_ref": {"kind", "repository", "ref"},
                    "github_release": {"kind", "repository", "tag"},
                    "pypi_release": {"kind", "name", "version"},
                    "databricks_workspace_files": {"kind", "host"},
                    "local_git_ref": {"kind", "ref"}}
_LIMIT = 16 * 1024 * 1024


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get_json(service: str, path: str) -> dict[str, Any]:
    hosts = {"github": "https://api.github.com", "pypi": "https://pypi.org"}
    headers = {"Accept": "application/json", "User-Agent": "odibi-anchor-workflow"}
    if service == "github":
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    try:
        with request.build_opener(_NoRedirect()).open(
            request.Request(hosts[service] + path, headers=headers), timeout=30,
        ) as response:
            raw = response.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError("oversized response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("response is not an object")
        return result
    except Exception as exc:
        from urllib.error import HTTPError

        from odibi_anchor._recovery import attach_recovery

        status = exc.code if isinstance(exc, HTTPError) else None
        retry_after = exc.headers.get("Retry-After") if isinstance(exc, HTTPError) and exc.headers else None
        detail = f"{type(exc).__name__}" + (f" {status}" if status is not None else "")
        raise attach_recovery(WorkflowError(
            "unavailable",
            f"{service} readback unavailable ({detail}) at {hosts[service]}{path}"
            + (f"; retry after {retry_after} s" if retry_after else "")
            + ". A transient failure is safe to retry with a new request_id; the destination was "
            "not changed by this read.",
        ), error_code="destination_readback_unavailable", context={
            "service": service, "endpoint": hosts[service] + path, "http_status": status,
            "retry_after": retry_after, "error_type": type(exc).__name__,
        }) from None


def _git(root: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise WorkflowError("unavailable", "local repository identity unavailable") from None


def _destination(state, session_state):
    destination = state["plan"]["destination"]
    kind = destination.get("kind")
    if kind not in DESTINATION_KEYS or set(destination) != DESTINATION_KEYS[kind]:
        raise WorkflowError("unavailable", "unsupported or incomplete destination contract")
    if kind.startswith("github_"):
        origin = _git(session_state.target_root, "remote", "get-url", "origin")
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?", origin)
        if not match or match[1] != destination["repository"]:
            raise WorkflowError("wrong_destination", "GitHub destination differs from runtime origin")
        ref = destination.get("ref", destination.get("tag"))
        if (not isinstance(ref, str) or not ref or len(ref) > 200
                or _git(session_state.target_root, "check-ref-format", "refs/tags/" + ref)):
            raise WorkflowError("wrong_destination", "invalid GitHub destination ref")
        if kind == "github_ref" and not ref.startswith(("refs/heads/", "refs/tags/")):
            raise WorkflowError("wrong_destination", "GitHub ref must be fully qualified")
    elif kind == "local_git_ref":
        ref = destination["ref"]
        if (not isinstance(ref, str) or len(ref) > 200 or not ref.startswith(("refs/heads/", "refs/tags/"))
                or _git(session_state.target_root, "check-ref-format", ref)):
            raise WorkflowError("wrong_destination", "local Git ref must be a fully qualified branch or tag")
    elif kind == "pypi_release":
        for key in ("name", "version"):
            if not isinstance(destination[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}", destination[key]):
                raise WorkflowError("wrong_destination", "invalid package identity")
    elif kind == "databricks_workspace_files":
        if not isinstance(destination["host"], str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+", destination["host"]):
            raise WorkflowError("wrong_destination", "workspace host must be an exact HTTPS origin")
    return destination


def _fresh(path, session_state, workflow_id, *, allow_unknown=False):
    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    reconciling = (allow_unknown and state["status"] == "blocked"
                   and (state.get("blocker") or {}).get("kind") == "outcome_unknown")
    if not state.get("qualification") or (state["status"] != "active" and not reconciling):
        raise WorkflowError("missing_evidence", "delivery requires active retained qualification")
    review = state["qualification"]["review"]
    if state["plan"]["risk"] == "high" and (
        review.get("separate_read_only_accepted_task") is not True
        or review.get("reviewer_authentication") != "none"
    ):
        raise WorkflowError("review_required", "high-risk delivery requires explicit task-separated review provenance")
    if collect_candidate(path, session_state=session_state, workflow_id=workflow_id) != state["candidate"]:
        raise WorkflowError("stale_evidence", "candidate changed after qualification")
    completion = producer_completion(path, session_state=session_state, producer=state["candidate"]["producer"])
    if state["qualification"].get("producer_terminal_record_sha256") != completion:
        raise WorkflowError("stale_evidence", "qualification lacks this exact producer terminal proof; requalify")
    environment = digest(runtime_environment())
    if any(check.get("environment_sha256") != environment
           for check in state["qualification"]["checks"] if check["method"] == "pytest"):
        raise WorkflowError("stale_evidence", "qualification environment changed before delivery")
    return state


def _approval_mismatch(mismatches, expected, *, operation, workflow_id, generation):
    """Refuse a reply that is not the exact challenge from the configured owner, naming why.

    The context names failed checks only; it never echoes the received reply text.
    """
    from odibi_anchor._recovery import attach_recovery, dispatcher_operation

    summary = "; ".join(item["check"] for item in mismatches)
    return attach_recovery(WorkflowError(
        "authority_required",
        f"human response does not match exact challenge and owner (failed: {summary}). Nothing was "
        f"approved. Request again and reply exactly: {expected}",
    ), error_code="delivery_approval_mismatch", context={
        "operation": operation, "workflow_id": workflow_id, "generation": generation,
        "expected_response": expected, "mismatches": mismatches,
    }, next_operations=[dispatcher_operation(
        "workflow", operation, kwargs={
            "workflow_id": workflow_id, "expected_generation": generation,
            "request_id": "<new-request-id>", "output_format": "dict",
        },
        reason="request the owner's reply again; the challenge is unchanged while the candidate is",
        requires_owner=True, retry_safety="not_idempotent",
    )])


def _owner_wait(result, *, operation, workflow_id, generation):
    """Report an unanswered owner request as a normal result; nothing was transitioned."""
    from odibi_anchor._recovery import dispatcher_operation

    status = result["status"]
    request = {"workflow_id": workflow_id, "expected_generation": generation,
               "request_id": "<new-request-id>", "output_format": "dict"}
    channels = result.get("channels") or ["chat_in_session"]
    provider = "databricks_in_session" if "databricks_in_session" in channels else "chat_in_session"
    check = dispatcher_operation(
        "workflow", operation, kwargs=request,
        reason=("check whether the owner replied; the open request is reused, never posted twice"
                if status == "pending" else "ask the owner again with a new request"),
        requires_owner=status != "pending",
        retry_safety="idempotent" if status == "pending" else "not_idempotent",
    )
    complete = dispatcher_operation(
        "workflow", operation, kwargs={**request, "provider": provider,
                                       "in_session_approval": result["approval_response"]},
        reason="complete only after the owner personally typed exactly approval_response in this session",
        requires_owner=True, retry_safety="single_use_challenge",
    )
    operations = {"pending": [check, complete], "approval_required": [complete],
                  "expired": [check, complete], "declined": [check]}[status]
    guidance = {
        "pending": "The owner has not replied yet. Check again, or complete with the owner's in-session reply.",
        "approval_required": "No remote owner channel is configured. Show approval_response; the owner "
                             "must type it exactly in this session.",
        "expired": "The owner request expired without a reply. Stop and report that approval is waiting.",
        "declined": "The owner declined this exact request. Nothing was approved.",
    }[status]
    return {"kind": "workflow_owner_approval", "schema_version": 1, "authority": "projection",
            "command": operation, "workflow_id": workflow_id, "generation": generation, **result,
            "guidance": guidance, "state_changed": False, "destination_mutation_performed": False,
            "next_operation": operations[0], "available_operations": operations}


def _owner_transition(path, prepare, *, operation, transition, verb, intro, session_state, workflow_id,
                      request_id, timeout_minutes, public_request, provider, in_session_approval,
                      wait_seconds):
    """Ask the owner through owner_approval; only an approved exact reply transitions."""
    from odibi_anchor.owner_approval import DEFAULT_WAIT_SECONDS, OwnerApprovalMismatch, request_owner_approval

    prepared = prepare(path, session_state=session_state, workflow_id=workflow_id)
    subject = prepared["subject"]
    try:
        result = request_owner_approval(
            operation=operation, subject=subject, prompt=intro + "\n" + canonical(subject), verb=verb,
            provider=provider, in_session_approval=in_session_approval, timeout_minutes=timeout_minutes,
            wait_seconds=DEFAULT_WAIT_SECONDS if wait_seconds is None else wait_seconds,
        )
    except OwnerApprovalMismatch as exc:
        raise _approval_mismatch(exc.mismatches, prepared["approval_response"], operation=operation,
                                 workflow_id=workflow_id, generation=subject["generation"]) from None
    if result["approval_response"] != prepared["approval_response"]:
        raise WorkflowError("authority_required", "owner challenge differs from the prepared challenge")
    if result["status"] != "approved":
        return _owner_wait(result, operation=operation, workflow_id=workflow_id,
                           generation=subject["generation"])
    if prepare(path, session_state=session_state, workflow_id=workflow_id) != prepared:
        raise WorkflowError("stale_evidence", f"{_STALE[operation]} changed during human approval")
    return transition_workflow(
        path, owner=workflow_owner(session_state), workflow_id=workflow_id,
        expected_generation=subject["generation"], request_id=request_id, operation=transition,
        payload={**subject, "actor_kind": "human", "owner": result["owner"],
                 "authority_ref": result["authority_ref"], "response_message_id": result["response_message_id"],
                 "owner_assurance": result["assurance"]},
        public_request=public_request,
    )


_STALE = {"request_delivery_approval": "candidate or qualification", "revoke_delivery": "workflow approval"}


def prepare_delivery(path, *, session_state, workflow_id):
    """Produce an exact, inspectable human challenge; grant no permission."""
    from odibi_anchor.owner_approval import challenge_for

    state = _fresh(path, session_state, workflow_id)
    if state["progress"] != "qualified":
        raise WorkflowError("invalid_transition", "approval requires qualified work")
    destination = _destination(state, session_state)
    subject = {"workflow_id": workflow_id, "generation": state["generation"],
               "owner": state["owner"], "plan_sha256": state["plan_sha256"],
               "candidate_sha256": digest(state["candidate"]),
               "qualification_sha256": digest(state["qualification"]),
               "destination": destination, "operation": _OPERATIONS[destination["kind"]],
               "review_assurance": state["qualification"]["review"].get("independence"),
               "reviewer_authentication": state["qualification"]["review"].get("reviewer_authentication", "none")}
    return {"kind": "workflow_delivery_request", "subject": subject,
            "approval_response": challenge_for(subject, "APPROVE")[1], "authority_granted": False}


def request_delivery_approval(path, *, session_state, workflow_id, request_id, timeout_minutes=30,
                              public_request=None, provider=None, in_session_approval=None,
                              wait_seconds=None):
    """Ask the owner to approve the exact candidate and destination, then CAS.

    Only an approved reply transitions. A pending, approval_required, expired or declined
    owner result is returned as a ``workflow_owner_approval`` packet and changes nothing.
    """
    return _owner_transition(
        path, prepare_delivery, operation="request_delivery_approval", transition="approve_delivery",
        verb="APPROVE", intro=("Authorize only this candidate and destination operation. Review task "
                               "separation does not authenticate a different reviewer."),
        session_state=session_state, workflow_id=workflow_id, request_id=request_id,
        timeout_minutes=timeout_minutes, public_request=public_request, provider=provider,
        in_session_approval=in_session_approval, wait_seconds=wait_seconds,
    )


def prepare_revocation(path, *, session_state, workflow_id):
    """Bind withdrawal to retained authority even when the candidate is stale."""
    from odibi_anchor.owner_approval import challenge_for

    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    if state["progress"] != "approved_for_delivery" or not state.get("approval"):
        raise WorkflowError("invalid_transition", "revocation requires an unused delivery approval")
    if (state.get("blocker") or {}).get("kind") == "outcome_unknown":
        raise WorkflowError("recovery_required", "reconcile the unknown delivery outcome first")
    subject = {"workflow_id": workflow_id, "generation": state["generation"],
               "owner": state["owner"], "plan_sha256": state["plan_sha256"],
               "candidate_sha256": digest(state["candidate"]),
               "approval_sha256": digest(state["approval"]),
               "destination": state["plan"]["destination"], "operation": "revoke_delivery"}
    return {"kind": "workflow_revocation_request", "subject": subject,
            "approval_response": challenge_for(subject, "REVOKE")[1], "authority_granted": False}


def request_delivery_revocation(path, *, session_state, workflow_id, request_id,
                                timeout_minutes=30, public_request=None, provider=None,
                                in_session_approval=None, wait_seconds=None):
    """Withdraw only the exact retained grant confirmed by the owner; same rules as approval."""
    return _owner_transition(
        path, prepare_revocation, operation="revoke_delivery", transition="revoke_delivery",
        verb="REVOKE", intro=("Revoke this exact unused delivery approval. This does not roll back any "
                              "destination or prove that an external operation did not occur."),
        session_state=session_state, workflow_id=workflow_id, request_id=request_id,
        timeout_minutes=timeout_minutes, public_request=public_request, provider=provider,
        in_session_approval=in_session_approval, wait_seconds=wait_seconds,
    )


def observe_destination(path, *, session_state, workflow_id, workspace_client=None):
    """Read current destination state without changing it or inferring permission."""
    state = _fresh(path, session_state, workflow_id, allow_unknown=True)
    if state["progress"] not in {"approved_for_delivery", "delivered"}:
        raise WorkflowError("authority_required", "delivery readback requires exact retained approval")
    destination = _destination(state, session_state)
    approval = state["approval"]
    if (approval.get("qualification_sha256") != digest(state["qualification"])
            or approval["destination"] != destination
            or approval["operation"] != _OPERATIONS[destination["kind"]]):
        raise WorkflowError("authority_required", "approval does not bind current qualified destination")
    candidate = state["candidate"]
    kind = destination["kind"]
    snapshot = candidate["snapshot"]
    if kind == "managed_artifacts" and candidate["kind"] == "managed_artifacts":
        # _fresh independently read every exact managed artifact byte and checked containment.
        observed = {"artifact_root": str(Path(session_state.artifact_root).resolve()), "files": snapshot}
    elif kind in {"github_ref", "github_release"} and candidate["kind"] == "git":
        if _git(session_state.target_root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise WorkflowError("unavailable", "Git destination requires committed clean candidate")
        repository = destination["repository"]
        ref = destination.get("ref", "refs/tags/" + destination.get("tag", ""))
        commit = _get_json("github", f"/repos/{repository}/commits/{quote(ref, safe='')}")
        if commit.get("sha") != snapshot["head_sha"]:
            raise WorkflowError("destination_unverified", "destination Git head differs from qualified head")
        observed = {"repository": repository, "ref": ref, "sha": commit["sha"]}
        if kind == "github_release":
            release = _get_json("github", f"/repos/{repository}/releases/tags/{quote(destination['tag'], safe='')}")
            if release.get("tag_name") != destination["tag"] or release.get("draft") is not False or not release.get("published_at"):
                raise WorkflowError("destination_unverified", "release is not published at exact tag")
            observed["release_id"] = release["id"]
            if _get_json("github", f"/repos/{repository}/commits/{quote(ref, safe='')}").get("sha") != commit["sha"]:
                raise WorkflowError("destination_unverified", "release tag moved during readback")
    elif kind == "local_git_ref" and candidate["kind"] == "git":
        if _git(session_state.target_root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise WorkflowError("unavailable", "Git destination requires committed clean candidate")
        ref = destination["ref"]
        try:
            sha = _git(session_state.target_root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        except WorkflowError:
            sha = None
        if sha != snapshot["head_sha"]:
            raise WorkflowError("destination_unverified", "local Git ref differs from qualified head")
        observed = {"ref": ref, "sha": sha}
    elif kind == "pypi_release" and candidate["kind"] == "managed_artifacts":
        release = _get_json("pypi", f"/pypi/{quote(destination['name'], safe='')}/{quote(destination['version'], safe='')}/json")
        info = release.get("info", {})
        normalized_name = re.sub(r"[-_.]+", "-", destination["name"]).lower()
        if (re.sub(r"[-_.]+", "-", str(info.get("name", ""))).lower() != normalized_name
                or info.get("version") != destination["version"]):
            raise WorkflowError("destination_unverified", "package name or version differs from approved destination")
        files = release.get("urls", [])
        observed = {}
        for name, expected in snapshot.items():
            matches = [item for item in files if item.get("filename") == Path(name).name]
            if (len(matches) != 1 or matches[0].get("yanked") is not False
                    or matches[0].get("digests", {}).get("sha256") != expected["sha256"]
                    or matches[0].get("size") != expected["size"]):
                raise WorkflowError("destination_unverified", "package release file digest or availability differs")
            observed[name] = {"sha256": expected["sha256"], "size": expected["size"]}
    elif kind == "databricks_workspace_files" and candidate["kind"] == "databricks_git_folder":
        if workspace_client is None:
            # Reuse only the configured repository provider's SDK capability. Do
            # not create a client from a caller-selected host or ambient URL.
            executor = getattr(session_state.repository_provider, "api_executor", None)
            workspace_client = getattr(executor, "workspace_client", None)
        if workspace_client is None or workspace_client.config.host.rstrip("/") != destination["host"]:
            raise WorkflowError("wrong_destination", "configured workspace host differs from approved host")
        observed = {}
        for name, expected in snapshot["content_changes"].items():
            remote = snapshot["workspace_path"].rstrip("/") + "/" + name
            status = workspace_client.workspace.get_status(remote)
            object_type = getattr(status.object_type, "value", status.object_type)
            if object_type != "FILE" or expected["final_sha256"] is None:
                raise WorkflowError("unavailable", "only existing workspace FILE byte readback is supported")
            with workspace_client.workspace.download(remote) as stream:
                data = stream.read(_LIMIT + 1)
            if (len(data) > _LIMIT or len(data) != expected["final_size"]
                    or hashlib.sha256(data).hexdigest() != expected["final_sha256"]):
                raise WorkflowError("destination_unverified", "workspace file bytes differ from qualified candidate")
            observed[name] = {"sha256": expected["final_sha256"], "size": len(data)}
        if not observed:
            raise WorkflowError("unavailable", "no workspace file delivery bytes to verify")
    else:
        raise WorkflowError("unavailable", "candidate has no supported destination byte contract")
    # Recheck local candidate and generation after remote I/O; observations are not locks.
    if _fresh(path, session_state, workflow_id, allow_unknown=True) != state:
        raise WorkflowError("stale_evidence", "workflow changed during destination observation")
    return {"plan_sha256": state["plan_sha256"], "candidate_sha256": digest(candidate),
            "destination": destination, "observed_identity": candidate["identity"],
            "method": "readback", "status": "satisfied", "observer": "anchor." + kind,
            "evidence_ref": digest(observed), "observed": observed,
            "consistency": "bounded readback, not a destination lock or atomic multi-object snapshot",
            "observed_at": datetime.now(UTC).isoformat(), "reconciliation": reconciliation_evidence(state)}
