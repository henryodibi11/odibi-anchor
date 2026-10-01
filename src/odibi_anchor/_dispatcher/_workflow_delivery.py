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
from odibi_anchor.codebase._workflow import WorkflowError, canonical, digest, read_workflow, transition_workflow

_OPERATIONS = {"managed_artifacts": "deliver_artifacts", "github_ref": "push",
               "github_release": "publish_release", "pypi_release": "publish_package",
               "databricks_workspace_files": "upload_workspace_files"}
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
        raise WorkflowError("unavailable", f"{service} readback unavailable ({type(exc).__name__})") from None


def _git(root: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise WorkflowError("unavailable", "local repository identity unavailable") from None


def _destination(state, session_state):
    destination = state["plan"]["destination"]
    kind = destination.get("kind")
    keys = {"managed_artifacts": {"kind"},
            "github_ref": {"kind", "repository", "ref"},
            "github_release": {"kind", "repository", "tag"},
            "pypi_release": {"kind", "name", "version"},
            "databricks_workspace_files": {"kind", "host"}}
    if kind not in keys or set(destination) != keys[kind]:
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


def prepare_delivery(path, *, session_state, workflow_id):
    """Produce an exact, inspectable human challenge; grant no permission."""
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
            "approval_response": "APPROVE " + digest(subject), "authority_granted": False}


def request_delivery_approval(path, *, session_state, workflow_id, request_id, timeout_minutes=5,
                              public_request=None):
    """Collect exact human response through configured owner transport, then CAS."""
    from odibi_anchor.human_input import request_human_input_record
    from odibi_anchor.human_input_owner import select_owner_approval_provider

    prepared = prepare_delivery(path, session_state=session_state, workflow_id=workflow_id)
    subject = prepared["subject"]
    provider = select_owner_approval_provider()
    message = ("Authorize only this candidate and destination operation. Review task separation "
               "does not authenticate a different reviewer.\n" + canonical(subject)
               + "\nReply exactly: " + prepared["approval_response"])
    response = request_human_input_record(message, timeout_minutes=timeout_minutes, transport=provider.transport)
    if (response.response != prepared["approval_response"]
            or response.response_user_id != provider.expected_owner_id
            or response.transport != provider.transport.name
            or not response.request_id or not response.response_message_id):
        raise WorkflowError("authority_required", "human response does not match exact challenge and owner")
    if prepare_delivery(path, session_state=session_state, workflow_id=workflow_id) != prepared:
        raise WorkflowError("stale_evidence", "candidate or qualification changed during human approval")
    return transition_workflow(
        path, owner=workflow_owner(session_state), workflow_id=workflow_id,
        expected_generation=subject["generation"], request_id=request_id, operation="approve_delivery",
        payload={**subject, "actor_kind": "human", "owner": provider.expected_owner_id,
                 "authority_ref": response.request_id, "response_message_id": response.response_message_id,
                 "owner_assurance": provider.assurance},
        public_request=public_request,
    )


def prepare_revocation(path, *, session_state, workflow_id):
    """Bind withdrawal to retained authority even when the candidate is stale."""
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
            "approval_response": "REVOKE " + digest(subject), "authority_granted": False}


def request_delivery_revocation(path, *, session_state, workflow_id, request_id,
                                timeout_minutes=5, public_request=None):
    """Withdraw only the exact retained grant confirmed by the configured owner."""
    from odibi_anchor.human_input import request_human_input_record
    from odibi_anchor.human_input_owner import select_owner_approval_provider

    prepared = prepare_revocation(path, session_state=session_state, workflow_id=workflow_id)
    subject = prepared["subject"]
    provider = select_owner_approval_provider()
    message = ("Revoke this exact unused delivery approval. This does not roll back any destination "
               "or prove that an external operation did not occur.\n" + canonical(subject)
               + "\nReply exactly: " + prepared["approval_response"])
    response = request_human_input_record(message, timeout_minutes=timeout_minutes, transport=provider.transport)
    if (response.response != prepared["approval_response"]
            or response.response_user_id != provider.expected_owner_id
            or response.transport != provider.transport.name
            or not response.request_id or not response.response_message_id):
        raise WorkflowError("authority_required", "human response does not match exact challenge and owner")
    if prepare_revocation(path, session_state=session_state, workflow_id=workflow_id) != prepared:
        raise WorkflowError("stale_evidence", "workflow approval changed during revocation")
    return transition_workflow(
        path, owner=workflow_owner(session_state), workflow_id=workflow_id,
        expected_generation=subject["generation"], request_id=request_id, operation="revoke_delivery",
        payload={**subject, "actor_kind": "human", "owner": provider.expected_owner_id,
                 "authority_ref": response.request_id, "response_message_id": response.response_message_id,
                 "owner_assurance": provider.assurance},
        public_request=public_request,
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
            "observed_at": datetime.now(UTC).isoformat(), "reconciliation": "satisfied"}
