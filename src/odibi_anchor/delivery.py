"""Bootstrap-free verification of local delivery evidence under declared policy."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from odibi_anchor._pr_readiness import (
    evaluate_pr_readiness,
    load_pr_config,
    pr_checks_to_evidence,
)
from odibi_anchor._repository_snapshot import RepositorySnapshot, validate_repository_snapshot
from odibi_anchor.operational._contract import (
    MAX_ITEMS,
    CapabilityRequest,
    ContractError,
    canonical_json,
    normalize_json,
)
from odibi_anchor.operational._git_snapshot import (
    PROVIDER_ID,
    capture_local_git_snapshot_evidence,
)
from odibi_anchor.planning._task_policy import EvidenceEntry

MAX_DECLARATIONS = 100
MAX_ARTIFACT_BYTES = 16_777_216
_HEX = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOP = {"schema_version", "target", "intended_pr_paths", "requirements", "attestations", "artifacts"}
_ANCHORS = ("configured_target_ref", "head_sha", "target_sha", "merge_base_sha",
            "index_fingerprint", "worktree_fingerprint", "managed_fingerprint")
_ADVISORY_PR_CHECKS = {f"subjective.{name}" for name in (
    "docstring-usefulness", "readability", "abstraction", "shared-value", "jargon",
    "usage-quality", "reviewer-comprehension",
)}


class DeliveryInputError(ValueError):
    """The delivery request violates the versioned input contract."""


class OutputLimitError(RuntimeError):
    """The complete delivery result cannot fit the output contract."""


@dataclass(frozen=True)
class DeliveryVerificationResult:
    """Strict JSON result for one declared-policy verification."""

    value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return normalize_json(self.value)


def _exact(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise DeliveryInputError(f"{name} must contain exactly {sorted(keys)}")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise DeliveryInputError(f"{name} must be a non-empty string")
    return value


def _path(value: Any, name: str) -> str:
    text = _text(value, name)
    path = PurePosixPath(text)
    if ("\0" in text or path.is_absolute() or text != path.as_posix()
            or ".." in path.parts or text in {"", "."}):
        raise DeliveryInputError(f"{name} must be a safe repository-relative POSIX path")
    return text


def _utc(value: Any, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryInputError(f"{name} must be canonical UTC") from exc
    canonical = parsed.isoformat()
    if (parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
            or text not in {canonical, canonical.replace("+00:00", "Z")}):
        raise DeliveryInputError(f"{name} must be canonical UTC")
    return parsed


def _freshness(value: Any, kind: str, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or "mode" not in value:
        raise DeliveryInputError(f"{name} has invalid freshness")
    mode = value["mode"]
    if mode == "repository_snapshot":
        item = _exact(value, {"mode", "anchors"}, name)
        anchors = _exact(item["anchors"], set(_ANCHORS), f"{name}.anchors")
        for key in _ANCHORS:
            anchor = anchors[key]
            if key in {"target_sha", "merge_base_sha"} and anchor is None:
                continue
            if key == "configured_target_ref":
                _text(anchor, f"{name}.{key}")
            elif type(anchor) is not str or not _HEX.fullmatch(anchor):
                raise DeliveryInputError(f"{name}.{key} must be lowercase 64-hex")
        return {"mode": mode, "anchors": anchors}
    if mode == "identity_bound":
        if kind != "work-item":
            raise DeliveryInputError("identity_bound is limited to work-item evidence")
        expected_keys = {"work_item_id", "provider", "provider_id", "provider_url", "fingerprint", "operations"}
        item = _exact(value, {"mode", "expected"}, name)
        expected = _exact(item["expected"], expected_keys, f"{name}.expected")
        for key in expected_keys - {"operations"}:
            _text(expected[key], f"{name}.expected.{key}")
        operations = expected["operations"]
        if not isinstance(operations, list) or not operations or any(type(x) is not str for x in operations):
            raise DeliveryInputError(f"{name}.expected.operations must be a non-empty string array")
        return {"mode": mode, "expected": expected}
    if mode == "observed_after":
        item = _exact(value, {"mode", "not_before"}, name)
        _utc(item["not_before"], f"{name}.not_before")
        return item
    raise DeliveryInputError(f"{name} has unsupported freshness mode")


def _parse(request: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = normalize_json(request)
        canonical_json(raw)
    except (ContractError, UnicodeError) as exc:
        raise DeliveryInputError(str(exc)) from exc
    top = _exact(raw, _TOP, "request")
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise DeliveryInputError("schema_version must equal integer 1")
    target = _exact(top["target"], {"worktree", "artifact_root"}, "target")
    for key in target:
        _text(target[key], f"target.{key}")
    if target["worktree"] != target["artifact_root"]:
        raise DeliveryInputError("version 1 requires target roots to be identical")
    paths = top["intended_pr_paths"]
    if not isinstance(paths, list) or len(paths) > MAX_ITEMS:
        raise DeliveryInputError(f"intended_pr_paths must be an array of at most {MAX_ITEMS} paths")
    paths = [_path(item, "intended_pr_paths item") for item in paths]
    if len(paths) != len(set(paths)):
        raise DeliveryInputError("intended_pr_paths must be unique")
    requirements = top["requirements"]
    if not isinstance(requirements, list) or len(requirements) > MAX_DECLARATIONS:
        raise DeliveryInputError("requirements must be an array of at most 100 items")
    normalized_requirements = []
    ids: set[str] = set()
    for index, raw_requirement in enumerate(requirements):
        requirement = _exact(raw_requirement, {"evidence", "freshness"}, f"requirements[{index}]")
        evidence = _exact(requirement["evidence"], {"id", "kind", "description", "required_before"}, "evidence")
        for key in ("id", "kind", "description"):
            _text(evidence[key], f"evidence.{key}")
        if evidence["required_before"] != "final" or evidence["id"] in ids:
            raise DeliveryInputError("requirements need unique IDs and required_before final")
        if evidence["id"].startswith(("pr-check:", "delivery:")):
            raise DeliveryInputError("requirement IDs must not use reserved mechanical namespaces")
        ids.add(evidence["id"])
        normalized_requirements.append({"evidence": evidence,
                                        "freshness": _freshness(requirement["freshness"], evidence["kind"], "freshness")})
    attestations = top["attestations"]
    if not isinstance(attestations, list) or len(attestations) > MAX_DECLARATIONS:
        raise DeliveryInputError("attestations must be an array of at most 100 items")
    attestation_ids: set[str] = set()
    normalized_attestations = []
    for item in attestations:
        entry = _exact(item, {"id", "kind", "status", "source", "observed_at", "provenance"}, "attestation")
        for key in ("id", "kind", "source"):
            _text(entry[key], f"attestation.{key}")
        _utc(entry["observed_at"], "attestation.observed_at")
        if (type(entry["status"]) is not str or entry["status"] not in {"pass", "fail", "unknown"}
                or not isinstance(entry["provenance"], Mapping)):
            raise DeliveryInputError("attestation has invalid status or provenance")
        if entry["id"] in attestation_ids:
            raise DeliveryInputError("attestation IDs must be unique")
        attestation_ids.add(entry["id"])
        normalized_attestations.append(entry)
    artifacts = top["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > MAX_DECLARATIONS:
        raise DeliveryInputError("artifacts must be an array of at most 100 items")
    artifact_ids: set[str] = set()
    artifact_paths: set[str] = set()
    normalized_artifacts = []
    for item in artifacts:
        artifact = _exact(item, {"id", "path", "sha256", "supports_evidence_ids", "freshness"}, "artifact")
        identifier, path = _text(artifact["id"], "artifact.id"), _path(artifact["path"], "artifact.path")
        supports = artifact["supports_evidence_ids"]
        if (identifier in artifact_ids or path in artifact_paths or type(artifact["sha256"]) is not str
                or not _DIGEST.fullmatch(artifact["sha256"]) or not isinstance(supports, list) or not supports
                or any(type(x) is not str for x in supports) or any(x not in ids for x in supports)
                or len(supports) != len(set(supports))):
            raise DeliveryInputError("artifact identity, digest, or evidence associations are invalid")
        freshness = _freshness(artifact["freshness"], "artifact", "freshness")
        if freshness["mode"] != "repository_snapshot":
            raise DeliveryInputError("artifact freshness must use repository_snapshot")
        artifact_ids.add(identifier)
        artifact_paths.add(path)
        normalized_artifacts.append({**artifact, "freshness": freshness})
    # Filesystem access occurs only after every JSON value has passed the bounded
    # structural contract above.
    for key, value in target.items():
        if "\0" in value:
            raise DeliveryInputError(f"target.{key} must be an existing canonical directory")
        try:
            resolved = Path(value).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DeliveryInputError(f"target.{key} must be an existing canonical directory") from exc
        if not resolved.is_dir() or str(resolved) != value:
            raise DeliveryInputError(f"target.{key} must be an existing canonical directory")
    return {**top, "target": target, "intended_pr_paths": paths, "requirements": normalized_requirements,
            "attestations": normalized_attestations, "artifacts": normalized_artifacts}


def _anchors(snapshot: RepositorySnapshot) -> dict[str, Any]:
    return {"configured_target_ref": snapshot.configured_target_ref, "head_sha": snapshot.head_sha,
            "target_sha": snapshot.target_sha, "merge_base_sha": snapshot.merge_base_sha,
            **{key: snapshot.provenance.get(key) for key in
               ("index_fingerprint", "worktree_fingerprint", "managed_fingerprint")}}


def _check(identifier: str, status: str, reason: str, description: str, evidence_ids: list[str] | tuple[str, ...] = (),
           provenance: str = "mechanical", freshness: Mapping[str, Any] | None = None,
           limitations: list[str] | tuple[str, ...] = (), blocking: bool | None = None) -> dict[str, Any]:
    return {"id": identifier, "status": status,
            "blocking": status in {"fail", "unknown"} if blocking is None else blocking,
            "reason_code": reason, "description": description, "evidence_ids": list(evidence_ids),
            "provenance_class": provenance, "freshness": dict(freshness or {}), "limitations": list(limitations)}


def _entry(check: Mapping[str, Any]) -> dict[str, Any]:
    return EvidenceEntry(f"delivery:{check['id']}", "delivery-mechanical",
                         "pass" if check["status"] in {"pass", "na"} else check["status"],
                         "odibi_anchor.delivery", datetime.now(timezone.utc).isoformat(),
                         {"reason_code": check["reason_code"], "description": check["description"]}).to_dict()


def _file_identity(value: os.stat_result, *, leaf: bool) -> tuple[int, ...]:
    identity = (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))
    return (*identity, value.st_size, value.st_mtime_ns) if leaf else identity


@dataclass
class _ArtifactHandle:
    root: str
    parts: tuple[str, ...]
    descriptors: list[int]
    identities: tuple[tuple[int, ...], ...]

    def path_is_stable(self) -> bool:
        """Re-walk the declared path without following links and compare identities."""
        reopened: list[int] = []
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            reopened.append(fd)
            current = [_file_identity(os.fstat(fd), leaf=False)]
            for index, part in enumerate(self.parts):
                leaf = index == len(self.parts) - 1
                flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if leaf else os.O_DIRECTORY)
                fd = os.open(part, flags, dir_fd=fd)
                reopened.append(fd)
                current.append(_file_identity(os.fstat(fd), leaf=leaf))
            return tuple(current) == self.identities
        except OSError:
            return False
        finally:
            for descriptor in reversed(reopened):
                os.close(descriptor)

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors.clear()


def _artifact(root: str, item: Mapping[str, Any]) -> tuple[str, str, _ArtifactHandle | None]:
    """Hash a stable regular file through component-wise no-follow descriptors."""
    descriptors: list[int] = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        parts = PurePosixPath(item["path"]).parts
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return "fail", "non_regular", None
        if before.st_size > MAX_ARTIFACT_BYTES:
            return "fail", "oversize", None
        digest = hashlib.sha256()
        total = 0
        while True:
            data = os.read(fd, min(131_072, MAX_ARTIFACT_BYTES - total + 1))
            if not data:
                break
            total += len(data)
            if total > MAX_ARTIFACT_BYTES:
                return "fail", "oversize", None
            digest.update(data)
        after = os.fstat(fd)
        def identity(value: os.stat_result) -> tuple[int, int, int, int]:
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

        if identity(before) != identity(after):
            return "fail", "stale", None
        if f"sha256:{digest.hexdigest()}" != item["sha256"]:
            return "fail", "digest_mismatch", None
        identities = tuple(_file_identity(os.fstat(descriptor), leaf=index == len(descriptors) - 1)
                           for index, descriptor in enumerate(descriptors))
        handle = _ArtifactHandle(root, tuple(parts), descriptors, identities)
        descriptors = []
        return "pass", "verified", handle
    except FileNotFoundError:
        return "fail", "missing", None
    except OSError as exc:
        return ("fail", "unsafe_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "unavailable", None)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _finish(value: dict[str, Any]) -> DeliveryVerificationResult:
    try:
        canonical_json(value)
    except ContractError as exc:
        raise OutputLimitError(str(exc)) from exc
    return DeliveryVerificationResult(value)


def _valid_work_item(entry: Mapping[str, Any]) -> bool:
    """Apply the production work-item attestation shape rules before admission."""
    from urllib.parse import urlsplit

    provenance = entry["provenance"]
    url = provenance.get("provider_url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    operations = provenance.get("operations")
    work_item_id = provenance.get("work_item_id")
    provider = provenance.get("provider")
    fingerprint = provenance.get("fingerprint")
    return bool(
        isinstance(work_item_id, str) and re.fullmatch(r"WI-\d{4}-\d{4}", work_item_id)
        and isinstance(provider, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", provider)
        and isinstance(provenance.get("provider_id"), str) and provenance["provider_id"].strip()
        and parsed is not None and parsed.scheme == "https" and parsed.netloc
        and entry["source"] == url
        and isinstance(fingerprint, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)
        and isinstance(operations, (list, tuple)) and bool(operations)
        and all(item in {"create_tasks", "update_tasks", "add_comment"} for item in operations)
        and isinstance(provenance.get("attestor"), str) and provenance["attestor"].strip()
    )


def verify_delivery(request: Mapping[str, Any]) -> DeliveryVerificationResult:
    """Recompute local readiness and evaluate supplied evidence without bootstrap."""
    if not isinstance(request, Mapping):
        raise DeliveryInputError("request must be a mapping")
    data = _parse(request)
    normalized_policy = {key: data[key] for key in
                         ("schema_version", "target", "intended_pr_paths", "requirements", "artifacts")}
    policy = {"normalized": normalized_policy,
              "sha256": f"sha256:{hashlib.sha256(canonical_json(normalized_policy)).hexdigest()}"}
    base = {"schema_version": 1, "eligible_under_declared_policy": False, "scope": "local_declared_policy",
            "declared_policy": policy, "snapshot_evidence": None, "pr_readiness": {"ready": False, "checks": []},
            "requirement_checks": [], "artifact_checks": [], "final_freshness_checks": [],
            "evidence": {"mechanical": [], "harness_attested": {"accepted": [], "rejected": []}},
            "limitations": [], "external_actions": {"fetched": False, "pushed": False, "pr_created": False,
                                                       "work_item_updated": False}}
    try:
        config = load_pr_config(data["target"]["artifact_root"])
    except Exception as exc:
        check = _check("configuration.valid", "fail", "invalid_configuration", "Repository PR configuration is invalid.")
        base["pr_readiness"]["checks"] = [check]
        base["requirement_checks"] = [_check(f"requirement:{x['evidence']['id']}", "unknown", "invalid_configuration",
                                                     x["evidence"]["description"]) for x in data["requirements"]]
        base["artifact_checks"] = [_check(f"artifact:{x['id']}", "unknown", "invalid_configuration",
                                                  "Artifact could not be verified.") for x in data["artifacts"]]
        base["final_freshness_checks"] = [_check("repository.snapshot", "unknown", "invalid_configuration",
                                                         "No repository capture was attempted.")]
        base["evidence"]["mechanical"] = [_entry(check)]
        base["evidence"]["harness_attested"]["rejected"] = [
            {"entry": x, "reason_code": "invalid_configuration"} for x in data["attestations"]]
        base["limitations"] = [f"Invalid repository configuration ({type(exc).__name__})."]
        return _finish(base)
    capture_request = CapabilityRequest(
        request_id="delivery-verification", capability_id="repository.local-change-snapshot",
        capability_contract_version="1.0", question="Capture current local delivery state.",
        target={"worktree": data["target"]["worktree"], "configured_target_ref": config["default_target_ref"]},
        scope={"worktree": data["target"]["worktree"],
               "change_classes": ["committed", "staged", "unstaged", "untracked"]},
        required_coverage={"local_change_classes": "all"}, freshness_requirement={"snapshot_bound": True},
        allowed_effects=("read",), pinned_provider_id=PROVIDER_ID,
    )
    snapshot, intake = capture_local_git_snapshot_evidence(capture_request, artifact_root=data["target"]["artifact_root"])
    base["snapshot_evidence"] = intake.to_dict()
    base["limitations"] = list(dict.fromkeys(intake.attempt.limitations +
                                               (() if intake.provider is None else intake.provider.limitations)))
    if snapshot is None:
        reason = intake.attempt.acquisition_outcome
        available = _check("repository.available", "unknown", reason, "Local repository snapshot is unavailable.")
        final = _check("repository.snapshot", "unknown", "repository_unavailable", "Snapshot freshness cannot be checked.")
        base["pr_readiness"]["checks"] = [available]
        base["requirement_checks"] = [_check(f"requirement:{x['evidence']['id']}", "unknown", "repository_unavailable",
                                                     x["evidence"]["description"]) for x in data["requirements"]]
        base["artifact_checks"] = [_check(f"artifact:{x['id']}", "unknown", "repository_unavailable",
                                                  "Artifact could not be verified.") for x in data["artifacts"]]
        base["final_freshness_checks"] = [final]
        base["evidence"]["mechanical"] = [_entry(available), _entry(final)]
        base["evidence"]["harness_attested"]["rejected"] = [{"entry": x, "reason_code": "no_snapshot"}
                                                                     for x in data["attestations"]]
        return _finish(base)
    current_anchors = _anchors(snapshot)
    captured = _utc(snapshot.captured_at.replace("+00:00", "Z"), "snapshot.captured_at")
    requirements = {item["evidence"]["id"]: item for item in data["requirements"]}
    accepted: list[EvidenceEntry] = []
    accepted_ids: set[str] = set()
    rejection_reasons: dict[str, str] = {}
    for raw in data["attestations"]:
        requirement = requirements.get(raw["id"])
        reason = "accepted"
        if requirement is None:
            reason = "undeclared"
        elif raw["kind"] != requirement["evidence"]["kind"]:
            reason = "kind_mismatch"
        elif raw["status"] != "pass":
            reason = raw["status"]
        elif not isinstance(raw["provenance"].get("attestor"), str) or not raw["provenance"]["attestor"].strip():
            reason = "malformed"
        elif _utc(raw["observed_at"], "observed_at") > captured:
            reason = "future"
        else:
            freshness = requirement["freshness"]
            if (freshness["mode"] == "repository_snapshot" and freshness["anchors"] != current_anchors) or (freshness["mode"] == "observed_after" and _utc(raw["observed_at"], "observed_at") < _utc(
                    freshness["not_before"], "not_before")):
                reason = "stale"
            elif freshness["mode"] == "identity_bound" and any(
                    raw["provenance"].get(key) != value for key, value in freshness["expected"].items()):
                reason = "identity_mismatch"
            elif freshness["mode"] == "identity_bound" and not _valid_work_item(raw):
                reason = "invalid_identity_shape"
        if reason == "accepted":
            entry = EvidenceEntry.from_dict(raw)
            accepted.append(entry)
            accepted_ids.add(raw["id"])
            base["evidence"]["harness_attested"]["accepted"].append({"entry": raw, "reason_code": "accepted"})
        else:
            rejection_reasons[raw["id"]] = reason
            base["evidence"]["harness_attested"]["rejected"].append({"entry": raw, "reason_code": reason})
    readiness = evaluate_pr_readiness(snapshot, config, attestations=tuple(accepted),
                                      intended_pr_paths=tuple(data["intended_pr_paths"]))
    converted_entries = pr_checks_to_evidence(readiness)
    mechanical_ids = {
        check.evidence_ids[0] for check in readiness.checks
        if check.evidence_ids == (f"pr-check:{check.id}",)
        and check.provenance.get("evidence_id") == f"pr-check:{check.id}"
    }
    evidence_entries = tuple(entry for entry in converted_entries if entry.id in mechanical_ids)
    wrapped = []
    for check in readiness.checks:
        provenance = "mechanical" if (
            check.evidence_ids == (f"pr-check:{check.id}",)
            and check.provenance.get("evidence_id") == f"pr-check:{check.id}"
        ) else "harness_attested"
        wrapped.append(_check(check.id, check.status, "ready" if check.status in {"pass", "na"} else "not_ready",
                              check.description, check.evidence_ids, provenance,
                              blocking=check.status == "fail" or (
                                  check.status == "unknown" and check.id not in _ADVISORY_PR_CHECKS)))
    base["evidence"]["mechanical"] = [item.to_dict() for item in evidence_entries]
    for item in data["requirements"]:
        evidence = item["evidence"]
        passed = evidence["id"] in accepted_ids
        rejection = rejection_reasons.get(evidence["id"])
        if passed:
            status, reason = "pass", "accepted"
        elif rejection in {"fail", "unknown"}:
            status, reason = rejection, rejection
        elif rejection in {"stale", "future", "identity_mismatch", "invalid_identity_shape"}:
            status, reason = "fail", rejection
        elif rejection is not None:
            status, reason = "unknown", rejection
        else:
            status, reason = "unknown", "missing"
        base["requirement_checks"].append(_check(f"requirement:{evidence['id']}", status, reason,
                                                         evidence["description"],
                                                         [evidence["id"]] if passed else [], "harness_attested",
                                                         item["freshness"]))
    incomplete: list[dict[str, Any]] = []
    if intake.attempt.truncated:
        incomplete.append(_check("repository.projection", "unknown", "incomplete",
                                 "Provider projection is incomplete."))
    if snapshot.target_sha is None or snapshot.merge_base_sha is None:
        incomplete.append(_check("repository.anchors", "unknown", "incomplete",
                                 "Target or merge-base snapshot anchors are missing."))
    exclusions = tuple(snapshot.provenance.get("managed_exclusions", ()))
    excluded_roots = tuple(item["path"] for item in exclusions)
    excluded_scope = [path for path in data["intended_pr_paths"]
                      if any(path == root or path.startswith(root + "/") for root in excluded_roots)]
    if excluded_scope:
        incomplete.append(_check("repository.managed-scope", "unknown", "incomplete",
                                 "Intended PR scope intersects provider-managed exclusions.",
                                 limitations=excluded_scope))
    wrapped.extend(incomplete)
    base["pr_readiness"] = {"ready": readiness.ready and not incomplete, "checks": wrapped}
    base["evidence"]["mechanical"].extend(_entry(check) for check in incomplete)
    artifact_handles: list[tuple[int, _ArtifactHandle]] = []
    for item in data["artifacts"]:
        if item["freshness"]["anchors"] != current_anchors:
            status, reason, handle = "fail", "stale", None
        else:
            status, reason, handle = _artifact(data["target"]["artifact_root"], item)
        base["artifact_checks"].append(_check(f"artifact:{item['id']}", status, reason,
                                                      "Declared artifact bytes match policy.", item["supports_evidence_ids"],
                                                      freshness=item["freshness"]))
        if handle is not None:
            artifact_handles.append((len(base["artifact_checks"]) - 1, handle))
    try:
        fresh, stale = validate_repository_snapshot(snapshot)
        final = _check("repository.snapshot", "pass" if fresh else "fail", "fresh" if fresh else "stale",
                       "Repository remains equal to the captured snapshot.", limitations=list(stale))
    except Exception:
        final = _check("repository.snapshot", "unknown", "stale_or_unavailable",
                       "Final repository freshness could not be established.")
    finally:
        for index, handle in artifact_handles:
            try:
                stable = handle.path_is_stable()
            finally:
                handle.close()
            if not stable:
                base["artifact_checks"][index].update(status="fail", blocking=True, reason_code="stale")
    base["final_freshness_checks"] = [final]
    base["evidence"]["mechanical"].append(_entry(final))
    all_checks = wrapped + base["requirement_checks"] + base["artifact_checks"] + [final]
    base["eligible_under_declared_policy"] = readiness.ready and not any(x["blocking"] for x in all_checks)
    return _finish(base)
