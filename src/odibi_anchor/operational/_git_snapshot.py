"""Caller-pinned managed evidence for one local Git change snapshot."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from odibi_anchor._repository_snapshot import RepositorySnapshot, capture_repository_snapshot

from ._contract import (
    MAX_STRING_CHARS,
    CapabilityRequest,
    CollectionAttempt,
    ContractError,
    EvidenceClaim,
    EvidenceIntake,
    ProviderDeclaration,
    canonical_json,
    utc_now,
)

CAPABILITY_ID = "repository.local-change-snapshot"
CAPABILITY_VERSION = "1.0"
PROVIDER_ID = "odibi-anchor.local-git-snapshot"

LOCAL_GIT_SNAPSHOT_PROVIDER = ProviderDeclaration(
    provider_id=PROVIDER_ID,
    implementation_version="1.0.0",
    capability_id=CAPABILITY_ID,
    supported_contract_versions=(CAPABILITY_VERSION,),
    execution_class="managed",
    network_requirement="none",
    required_effects=("read",),
    coverage_semantics="bounded",
    limitations=(
        "Local Git state only; remote freshness is not validated",
        "Git plumbing may create unreachable objects but does not change worktree, index, or refs",
    ),
    maturity="initial",
)

_DEFAULT_LIMITS = {"max_paths": 500, "max_ranges": 500, "max_commands": 200}
_REQUEST_BYTE_LIMIT = 131_072
_ATTESTATION_BYTE_LIMIT = 32_768
_PROJECTION_BYTE_LIMIT = 262_144


def _encoded_size(value: Any, *, label: str) -> int:
    try:
        return len(canonical_json(value))
    except UnicodeEncodeError as exc:
        raise ContractError(f"{label} must contain UTF-8 JSON text") from exc


def _require_supported_request(request: CapabilityRequest) -> None:
    worktree = request.target["worktree"]
    expected_scope = {
        "worktree": worktree,
        "change_classes": ["committed", "staged", "unstaged", "untracked"],
    }
    if request.to_dict()["scope"] != expected_scope:
        raise ContractError("scope must request all four local change classes for the target worktree")
    if request.to_dict()["required_coverage"] != {"local_change_classes": "all"}:
        raise ContractError("required_coverage must require all local change classes")
    if request.to_dict()["freshness_requirement"] != {"snapshot_bound": True}:
        raise ContractError("freshness_requirement must be snapshot_bound")
    if request.exclusions:
        raise ContractError("request exclusions are not supported by this capability version")
    if _encoded_size(request.to_dict(), label="request") > _REQUEST_BYTE_LIMIT:
        raise ContractError(f"request exceeds the {_REQUEST_BYTE_LIMIT}-byte provider limit")


def _request_values(request: CapabilityRequest) -> tuple[Path, str, dict[str, int]]:
    if not isinstance(request, CapabilityRequest):
        raise ContractError("request must use CapabilityRequest")
    if request.capability_id != CAPABILITY_ID or request.capability_contract_version != CAPABILITY_VERSION:
        raise ContractError("unsupported local Git snapshot capability")
    if request.pinned_provider_id != PROVIDER_ID:
        raise ContractError("local Git snapshot request must pin the provider")
    if len(request.request_id) + len(":snapshot") > MAX_STRING_CHARS:
        raise ContractError("request_id is too long for derived evidence identifiers")
    if set(request.target) != {"worktree", "configured_target_ref"}:
        raise ContractError("target must contain worktree and configured_target_ref")
    worktree = request.target["worktree"]
    target_ref = request.target["configured_target_ref"]
    if not isinstance(worktree, str) or not worktree.strip():
        raise ContractError("target worktree must be a non-empty string")
    if not isinstance(target_ref, str) or not target_ref.strip():
        raise ContractError("configured_target_ref must be a non-empty string")
    if target_ref.startswith("-") or any(char.isspace() or char in "~^:?*[\\" for char in target_ref):
        raise ContractError("unsafe configured target ref")
    _require_supported_request(request)
    limits = dict(_DEFAULT_LIMITS)
    unknown = set(request.resource_constraints) - set(limits)
    if unknown:
        raise ContractError(f"unsupported resource constraints: {sorted(unknown)}")
    for name, value in request.resource_constraints.items():
        if type(value) is not int or not 1 <= value <= 1_000:
            raise ContractError(f"{name} must be an integer from 1 through 1000")
        limits[name] = value
    return Path(worktree), target_ref, limits


def _git_worktree_available(worktree: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(worktree), "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    try:
        return Path(os.fsdecode(result.stdout).strip()).resolve(strict=True) == worktree.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return False


def _failed_intake(
    request: CapabilityRequest,
    *,
    outcome: str,
    started_at: str,
    limitation: str,
    error: Mapping[str, str] | None = None,
) -> EvidenceIntake:
    completeness = "unknown" if outcome == "error" else "not_applicable"
    return EvidenceIntake(
        request=request,
        provider=LOCAL_GIT_SNAPSHOT_PROVIDER,
        attempt=CollectionAttempt(
            attempt_id=f"{request.request_id}:attempt",
            request_id=request.request_id,
            provider_id=PROVIDER_ID,
            provider_binding="caller_pinned",
            provenance_kind="mechanical",
            acquisition_outcome=outcome,
            completeness=completeness,
            started_at=started_at,
            completed_at=utc_now(),
            executor_identity={"component": "odibi_anchor._repository_snapshot"},
            source_identity={"target": dict(request.target)},
            environment_identity={"scope": "local_only", "remote_validated": False},
            limitations=(limitation,),
            error=error,
        ),
        assessment="unknown",
    )


def _project(
    items: tuple[Any, ...], limit: int, remaining_bytes: list[int], *, label: str,
) -> tuple[list[Any], bool]:
    projected: list[Any] = []
    for item in items[:limit]:
        try:
            size = _encoded_size(item, label=label)
        except ContractError:
            return projected, True
        if size > remaining_bytes[0]:
            return projected, True
        projected.append(item)
        remaining_bytes[0] -= size
    return projected, len(projected) < len(items)


def capture_local_git_snapshot_evidence(
    request: CapabilityRequest,
    *,
    artifact_root: str | os.PathLike[str] | None = None,
    caller_attestation: Mapping[str, Any] | None = None,
    read_allowed: bool = True,
) -> tuple[RepositorySnapshot | None, EvidenceIntake]:
    """Capture one existing local snapshot and normalize it without provider selection.

    Returns:
        The exact snapshot used for normalization, when captured, and its immutable
        provider-neutral evidence intake.

    Raises:
        ContractError: The request does not match the fixed capability boundary.
    """
    worktree, target_ref, limits = _request_values(request)
    if type(read_allowed) is not bool:
        raise ContractError("read_allowed must be boolean")
    started_at = utc_now()
    if not read_allowed or "read" not in request.allowed_effects:
        return None, _failed_intake(
            request, outcome="denied", started_at=started_at,
            limitation="Local repository read permission was denied",
        )
    if not _git_worktree_available(worktree):
        return None, _failed_intake(
            request, outcome="unavailable", started_at=started_at,
            limitation="The target is not an available canonical local Git worktree",
        )
    if caller_attestation is not None and not isinstance(caller_attestation, Mapping):
        raise ContractError("caller_attestation must be a mapping")
    attestation = dict(caller_attestation or {})
    if _encoded_size(attestation, label="caller_attestation") > _ATTESTATION_BYTE_LIMIT:
        raise ContractError(f"caller_attestation exceeds the {_ATTESTATION_BYTE_LIMIT}-byte provider limit")
    try:
        snapshot = capture_repository_snapshot(
            worktree,
            target_ref,
            artifact_root=artifact_root,
            caller_attestation=attestation,
        )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        message = str(exc).encode("utf-8", "replace").decode("utf-8")[:1_000]
        return None, _failed_intake(
            request,
            outcome="error",
            started_at=started_at,
            limitation="Local Git snapshot capture failed after eligibility",
            error={"type": type(exc).__name__, "message": message},
        )

    paths = {
        "staged": snapshot.staged_paths,
        "unstaged": snapshot.unstaged_paths,
        "untracked": snapshot.untracked_paths,
        "changed": snapshot.changed_paths,
    }
    commands = tuple(snapshot.provenance.get("commands", ()))
    ranges = tuple(snapshot.changed_line_ranges)
    remaining_bytes = [_PROJECTION_BYTE_LIMIT]
    projected_paths: dict[str, list[Any]] = {}
    truncated_fields: list[str] = []
    for name, values in paths.items():
        projected, truncated = _project(
            values, limits["max_paths"], remaining_bytes, label=f"{name} path",
        )
        projected_paths[name] = projected
        if truncated:
            truncated_fields.append(name)
    range_values = tuple(
        {"path": item.path, "kind": item.kind, "start": item.start, "end": item.end}
        for item in ranges
    )
    projected_ranges, ranges_truncated = _project(
        range_values, limits["max_ranges"], remaining_bytes, label="changed line range",
    )
    if ranges_truncated:
        truncated_fields.append("changed_line_ranges")
    projected_commands, commands_truncated = _project(
        commands, limits["max_commands"], remaining_bytes, label="Git command provenance",
    )
    if commands_truncated:
        truncated_fields.append("commands")
    missing_anchors = [
        name for name, value in (
            ("target_sha", snapshot.target_sha),
            ("merge_base_sha", snapshot.merge_base_sha),
            ("committed_diff_range", snapshot.committed_diff_range),
        ) if value is None
    ]
    managed_exclusion_records = tuple(
        {"path": str(item.get("path")), "reason": str(item.get("reason"))}
        for item in snapshot.provenance.get("managed_exclusions", ())
        if isinstance(item, Mapping) and item.get("path") and item.get("reason")
    )
    managed_exclusions = tuple(item["path"] for item in managed_exclusion_records)
    limitations = []
    if missing_anchors:
        limitations.append(f"Missing local anchors: {', '.join(missing_anchors)}")
    if truncated_fields:
        limitations.append(f"Normalized evidence truncated: {', '.join(truncated_fields)}")
    if managed_exclusions:
        limitations.append("The snapshot omitted untracked managed-artifact paths")
    completeness = "partial" if limitations else "complete"
    coverage = {
        f"{name}_paths": projected_paths[name]
        for name in paths
    }
    coverage.update({
        "path_counts": {name: len(values) for name, values in paths.items()},
        "changed_line_ranges": projected_ranges,
        "changed_line_range_count": len(ranges),
        "local_conflict_result": snapshot.local_conflict_result,
    })
    freshness = {
        "captured_at": snapshot.captured_at,
        "head_sha": snapshot.head_sha,
        "target_sha": snapshot.target_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "index_fingerprint": snapshot.provenance.get("index_fingerprint"),
        "worktree_fingerprint": snapshot.provenance.get("worktree_fingerprint"),
        "managed_fingerprint": snapshot.provenance.get("managed_fingerprint"),
    }
    attempt = CollectionAttempt(
        attempt_id=f"{request.request_id}:attempt",
        request_id=request.request_id,
        provider_id=PROVIDER_ID,
        provider_binding="caller_pinned",
        provenance_kind="mechanical",
        acquisition_outcome="succeeded",
        completeness=completeness,
        started_at=started_at,
        completed_at=utc_now(),
        executor_identity={"component": "odibi_anchor._repository_snapshot"},
        coverage=coverage,
        limits={
            **limits,
            "projection_byte_limit": _PROJECTION_BYTE_LIMIT,
            "projection_bytes_used": _PROJECTION_BYTE_LIMIT - remaining_bytes[0],
            "command_count": len(commands),
            "commands": projected_commands,
            "managed_exclusions": list(managed_exclusion_records),
            "truncated_fields": truncated_fields,
        },
        additional_exclusions=managed_exclusions,
        truncated=bool(truncated_fields),
        source_identity={
            "target_worktree": snapshot.target_worktree,
            "branch": snapshot.branch,
            "head_sha": snapshot.head_sha,
            "configured_target_ref": snapshot.configured_target_ref,
            "target_sha": snapshot.target_sha,
            "merge_base_sha": snapshot.merge_base_sha,
            "committed_diff_range": snapshot.committed_diff_range,
        },
        environment_identity={
            "scope": snapshot.provenance.get("scope"),
            "remote_validated": snapshot.provenance.get("remote_validated"),
            "git_version": snapshot.provenance.get("git_version"),
        },
        limitations=tuple(limitations),
    )
    claim = EvidenceClaim(
        claim_id=f"{request.request_id}:snapshot",
        attempt_id=attempt.attempt_id,
        statement="The local Git change snapshot was captured at the recorded freshness anchor.",
        polarity="presence",
        epistemic_class="direct_observation",
        source_locator={
            "target_worktree": snapshot.target_worktree,
            "head_sha": snapshot.head_sha,
        },
        coverage_basis={
            "scope": "local_only",
            "attempt_coverage": "collection_attempt.coverage",
            "change_classes": ["committed", "staged", "unstaged", "untracked"],
        },
        freshness_anchor=freshness,
        uncertainty=tuple(limitations),
        provider_metadata={PROVIDER_ID: {
            "schema_version": snapshot.schema_version,
            "git_version": snapshot.provenance.get("git_version"),
            "caller_attestation": snapshot.provenance.get("caller_attestation", {}),
        }},
    )
    intake = EvidenceIntake(
        request=request,
        provider=LOCAL_GIT_SNAPSHOT_PROVIDER,
        attempt=attempt,
        claims=(claim,),
        assessment="unknown",
    )
    return snapshot, intake
