"""Focused contract tests for bootstrap-independent delivery verification."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from odibi_anchor import delivery
from odibi_anchor._pr_readiness import PRCheck, PRReadinessResult
from odibi_anchor._repository_snapshot import capture_repository_snapshot
from odibi_anchor.delivery import DeliveryInputError, verify_delivery


def request(root, **updates):
    """Build the smallest version-1 request rooted at an existing directory."""
    value = {
        "schema_version": 1,
        "target": {"worktree": str(root), "artifact_root": str(root)},
        "intended_pr_paths": [],
        "requirements": [],
        "attestations": [],
        "artifacts": [],
    }
    value.update(updates)
    return value


def _git(root):
    subprocess.run(["git", "init", "--object-format=sha256", "-b", "main"], cwd=root,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)


def _captured(monkeypatch, root):
    snapshot = capture_repository_snapshot(root, artifact_root=root)
    attempt = SimpleNamespace(truncated=False, completeness="complete", limitations=(),
                              acquisition_outcome="succeeded")
    intake = SimpleNamespace(attempt=attempt, provider=None, to_dict=lambda: {"attempt": {
        "acquisition_outcome": "succeeded"}})
    calls = []
    monkeypatch.setattr(delivery, "capture_local_git_snapshot_evidence",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or (snapshot, intake))
    monkeypatch.setattr(delivery, "evaluate_pr_readiness",
                        lambda seen, *_args, **_kwargs: calls.append(seen) or PRReadinessResult(True, ()))
    monkeypatch.setattr(delivery, "validate_repository_snapshot",
                        lambda seen: calls.append(seen) or (seen is snapshot, ()))
    return snapshot, calls


def _anchors(snapshot):
    return delivery._anchors(snapshot)


def _requirement(snapshot, *, mode="repository_snapshot", kind="test"):
    freshness = ({"mode": mode, "anchors": _anchors(snapshot)} if mode == "repository_snapshot"
                 else {"mode": "observed_after", "not_before": "2020-01-01T00:00:00Z"})
    return {"evidence": {"id": "tests.focused", "kind": kind, "description": "tests", "required_before": "final"},
            "freshness": freshness}


def _attestation(snapshot, **updates):
    value = {"id": "tests.focused", "kind": "test", "status": "pass", "source": "harness",
             "observed_at": snapshot.captured_at.replace("+00:00", "Z"), "provenance": {"attestor": "ci"}}
    value.update(updates)
    return value


def test_non_git_root_returns_structured_no_snapshot(tmp_path):
    result = verify_delivery(request(tmp_path)).to_dict()

    assert result["eligible_under_declared_policy"] is False
    assert result["snapshot_evidence"]["attempt"]["acquisition_outcome"] == "unavailable"
    assert result["pr_readiness"]["checks"][0]["id"] == "repository.available"
    assert result["final_freshness_checks"][0]["reason_code"] == "repository_unavailable"


def test_invalid_configuration_is_structured_and_pre_capture(tmp_path):
    config = tmp_path / ".odibi-anchor"
    config.mkdir()
    (config / "pr.json").write_text("{}", encoding="utf-8")

    result = verify_delivery(request(tmp_path)).to_dict()

    assert result["snapshot_evidence"] is None
    assert result["pr_readiness"]["checks"][0]["reason_code"] == "invalid_configuration"
    assert result["final_freshness_checks"][0]["status"] == "unknown"


def test_invalid_configuration_never_invokes_provider(monkeypatch, tmp_path):
    config = tmp_path / ".odibi-anchor"
    config.mkdir()
    (config / "pr.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(delivery, "capture_local_git_snapshot_evidence",
                        lambda *_args, **_kwargs: pytest.fail("provider must not run"))

    verify_delivery(request(tmp_path))


@pytest.mark.parametrize("change", [
    {"extra": True},
    {"schema_version": True},
    {"intended_pr_paths": ["../escape"]},
])
def test_strict_malformed_requests_fail_as_input(tmp_path, change):
    value = request(tmp_path)
    value.update(change)
    with pytest.raises(DeliveryInputError):
        verify_delivery(value)


def test_capture_once_and_same_snapshot_reaches_readiness_and_validation(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, calls = _captured(monkeypatch, tmp_path)

    verify_delivery(request(tmp_path))

    assert len(calls) == 3
    assert calls[1] is snapshot
    assert calls[2] is snapshot


def test_normal_managed_exclusions_do_not_make_partial_projection_block(monkeypatch, tmp_path):
    _git(tmp_path)
    _, calls = _captured(monkeypatch, tmp_path)
    calls[0:0] = []
    original = delivery.capture_local_git_snapshot_evidence

    def partial(*args, **kwargs):
        snapshot, intake = original(*args, **kwargs)
        intake.attempt.completeness = "partial"
        return snapshot, intake

    monkeypatch.setattr(delivery, "capture_local_git_snapshot_evidence", partial)
    result = verify_delivery(request(tmp_path)).to_dict()

    assert result["eligible_under_declared_policy"] is True
    assert all(check["id"] != "repository.projection" for check in result["pr_readiness"]["checks"])


def test_truncated_projection_and_managed_scope_are_blocking(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    attempt = SimpleNamespace(truncated=True, completeness="partial", limitations=(),
                              acquisition_outcome="succeeded")
    intake = SimpleNamespace(attempt=attempt, provider=None,
                             to_dict=lambda: {"attempt": {"acquisition_outcome": "succeeded"}})
    monkeypatch.setattr(delivery, "capture_local_git_snapshot_evidence", lambda *_a, **_k: (snapshot, intake))

    result = verify_delivery(request(tmp_path, intended_pr_paths=["specs/change.md"])).to_dict()

    checks = {check["id"]: check for check in result["pr_readiness"]["checks"]}
    assert checks["repository.projection"]["reason_code"] == "incomplete"
    assert checks["repository.managed-scope"]["blocking"] is True
    assert result["eligible_under_declared_policy"] is False


def test_missing_snapshot_anchors_are_blocking(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    snapshot = replace(snapshot, target_sha=None, merge_base_sha=None)
    attempt = SimpleNamespace(truncated=False, completeness="partial", limitations=(),
                              acquisition_outcome="succeeded")
    intake = SimpleNamespace(attempt=attempt, provider=None,
                             to_dict=lambda: {"attempt": {"acquisition_outcome": "succeeded"}})
    monkeypatch.setattr(delivery, "capture_local_git_snapshot_evidence", lambda *_a, **_k: (snapshot, intake))

    result = verify_delivery(request(tmp_path)).to_dict()

    assert any(check["id"] == "repository.anchors" and check["blocking"]
               for check in result["pr_readiness"]["checks"])


@pytest.mark.parametrize("updates,expected_status,expected_reason", [
    ({"status": "fail"}, "fail", "fail"),
    ({"status": "unknown"}, "unknown", "unknown"),
    ({"kind": "review"}, "unknown", "kind_mismatch"),
    ({"observed_at": "2999-01-01T00:00:00Z"}, "fail", "future"),
])
def test_required_attestation_rejections_are_distinct(monkeypatch, tmp_path, updates,
                                                       expected_status, expected_reason):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    result = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)],
                                     attestations=[_attestation(snapshot, **updates)])).to_dict()

    check = result["requirement_checks"][0]
    assert (check["status"], check["reason_code"]) == (expected_status, expected_reason)
    assert result["evidence"]["harness_attested"]["accepted"] == []


def test_unhashable_status_is_contract_invalid(tmp_path):
    value = request(tmp_path, attestations=[{
        "id": "test", "kind": "test", "status": [], "source": "harness",
        "observed_at": "2026-01-01T00:00:00Z", "provenance": {},
    }])
    with pytest.raises(DeliveryInputError):
        verify_delivery(value)


def test_reserved_mechanical_evidence_id_is_contract_invalid(tmp_path):
    requirement = {"evidence": {"id": "pr-check:snapshot.fresh", "kind": "test",
                                "description": "collision", "required_before": "final"},
                   "freshness": {"mode": "observed_after", "not_before": "2026-01-01T00:00:00Z"}}
    with pytest.raises(DeliveryInputError, match="reserved mechanical"):
        verify_delivery(request(tmp_path, requirements=[requirement]))


def test_intended_path_uses_generic_container_bound(tmp_path):
    paths = [f"src/{index}.py" for index in range(101)]
    result = verify_delivery(request(tmp_path, intended_pr_paths=paths)).to_dict()
    assert result["snapshot_evidence"]["attempt"]["acquisition_outcome"] == "unavailable"

    with pytest.raises(DeliveryInputError):
        verify_delivery(request(tmp_path, intended_pr_paths=[f"src/{index}.py" for index in range(1001)]))


def test_missing_and_stale_attestations_are_distinct(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    missing = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)])).to_dict()
    stale_requirement = _requirement(snapshot)
    stale_requirement["freshness"]["anchors"]["head_sha"] = "0" * 64
    stale = verify_delivery(request(tmp_path, requirements=[stale_requirement],
                                    attestations=[_attestation(snapshot)])).to_dict()

    assert missing["requirement_checks"][0]["reason_code"] == "missing"
    assert (stale["requirement_checks"][0]["status"], stale["requirement_checks"][0]["reason_code"]) == ("fail", "stale")


def test_observed_after_accepts_boundary_and_rejects_older(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    requirement = _requirement(snapshot, mode="observed_after")
    accepted = verify_delivery(request(
        tmp_path, requirements=[requirement],
        attestations=[_attestation(snapshot, observed_at="2020-01-01T00:00:00Z")],
    )).to_dict()
    stale = verify_delivery(request(
        tmp_path, requirements=[requirement],
        attestations=[_attestation(snapshot, observed_at="2019-12-31T23:59:59Z")],
    )).to_dict()

    assert accepted["requirement_checks"][0]["status"] == "pass"
    assert stale["requirement_checks"][0]["reason_code"] == "stale"


def test_canonical_production_utc_offset_is_accepted(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    result = verify_delivery(request(
        tmp_path, requirements=[_requirement(snapshot)],
        attestations=[_attestation(snapshot, observed_at=snapshot.captured_at)],
    )).to_dict()
    assert result["requirement_checks"][0]["status"] == "pass"


@pytest.mark.parametrize("content,digest,expected", [
    (None, "sha256:" + "0" * 64, "missing"),
    (b"actual", "sha256:" + "0" * 64, "digest_mismatch"),
])
def test_artifact_failure_classes(monkeypatch, tmp_path, content, digest, expected):
    _git(tmp_path)
    artifact = tmp_path / "specs" / "proof.bin"
    artifact.parent.mkdir()
    if content is not None:
        artifact.write_bytes(content)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    declaration = {"id": "proof", "path": "specs/proof.bin", "sha256": digest,
                   "supports_evidence_ids": ["tests.focused"],
                   "freshness": {"mode": "repository_snapshot", "anchors": _anchors(snapshot)}}
    result = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)],
                                     artifacts=[declaration])).to_dict()
    assert result["artifact_checks"][0]["reason_code"] == expected


@pytest.mark.parametrize("leaf_kind,expected", [("directory", "non_regular"), ("symlink", "unsafe_path")])
def test_artifact_rejects_non_regular_and_symlink(monkeypatch, tmp_path, leaf_kind, expected):
    _git(tmp_path)
    path = tmp_path / "proof"
    if leaf_kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target"
        target.write_bytes(b"proof")
        path.symlink_to(target)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    declaration = {"id": "proof", "path": "proof", "sha256": "sha256:" + "0" * 64,
                   "supports_evidence_ids": ["tests.focused"],
                   "freshness": {"mode": "repository_snapshot", "anchors": _anchors(snapshot)}}

    result = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)],
                                     artifacts=[declaration])).to_dict()
    assert result["artifact_checks"][0]["reason_code"] == expected


def test_artifact_replacement_during_final_validation_is_stale(monkeypatch, tmp_path):
    _git(tmp_path)
    path = tmp_path / "proof.bin"
    path.write_bytes(b"proof")
    snapshot, _ = _captured(monkeypatch, tmp_path)
    digest = "sha256:" + hashlib.sha256(b"proof").hexdigest()
    declaration = {"id": "proof", "path": "proof.bin", "sha256": digest,
                   "supports_evidence_ids": ["tests.focused"],
                   "freshness": {"mode": "repository_snapshot", "anchors": _anchors(snapshot)}}

    def replace_artifact(seen):
        assert seen is snapshot
        path.unlink()
        path.write_bytes(b"proof")
        return True, ()

    monkeypatch.setattr(delivery, "validate_repository_snapshot", replace_artifact)
    result = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)],
                                     artifacts=[declaration])).to_dict()
    assert result["artifact_checks"][0]["reason_code"] == "stale"


def test_artifact_size_and_anchor_mismatch_block(monkeypatch, tmp_path):
    _git(tmp_path)
    path = tmp_path / "proof.bin"
    path.write_bytes(b"proof")
    snapshot, _ = _captured(monkeypatch, tmp_path)
    declaration = {"id": "proof", "path": "proof.bin",
                   "sha256": "sha256:" + hashlib.sha256(b"proof").hexdigest(),
                   "supports_evidence_ids": ["tests.focused"],
                   "freshness": {"mode": "repository_snapshot", "anchors": _anchors(snapshot)}}
    monkeypatch.setattr(delivery, "MAX_ARTIFACT_BYTES", 4)
    oversize = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)],
                                       artifacts=[declaration])).to_dict()
    declaration["freshness"]["anchors"]["managed_fingerprint"] = "0" * 64
    stale = verify_delivery(request(tmp_path, requirements=[_requirement(snapshot)],
                                    artifacts=[declaration])).to_dict()

    assert oversize["artifact_checks"][0]["reason_code"] == "oversize"
    assert stale["artifact_checks"][0]["reason_code"] == "stale"


def test_no_snapshot_does_not_open_artifact_or_run_readiness(monkeypatch, tmp_path):
    attempt = SimpleNamespace(truncated=False, completeness="not_applicable", limitations=(),
                              acquisition_outcome="unavailable")
    intake = SimpleNamespace(attempt=attempt, provider=None,
                             to_dict=lambda: {"attempt": {"acquisition_outcome": "unavailable"}})
    monkeypatch.setattr(delivery, "capture_local_git_snapshot_evidence", lambda *_a, **_k: (None, intake))
    monkeypatch.setattr(delivery, "_artifact", lambda *_a, **_k: pytest.fail("artifact must not open"))
    monkeypatch.setattr(delivery, "evaluate_pr_readiness", lambda *_a, **_k: pytest.fail("readiness must not run"))
    anchors = {key: ("main" if key == "configured_target_ref" else "0" * 64) for key in delivery._ANCHORS}
    declaration = {"id": "proof", "path": "missing", "sha256": "sha256:" + "0" * 64,
                   "supports_evidence_ids": ["tests.focused"],
                   "freshness": {"mode": "repository_snapshot", "anchors": anchors}}
    requirement = {"evidence": {"id": "tests.focused", "kind": "test", "description": "tests",
                                "required_before": "final"},
                   "freshness": {"mode": "repository_snapshot", "anchors": anchors}}

    result = verify_delivery(request(tmp_path, requirements=[requirement], artifacts=[declaration])).to_dict()
    assert result["artifact_checks"][0]["reason_code"] == "repository_unavailable"


def test_policy_digest_excludes_attestations(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    base = request(tmp_path, requirements=[_requirement(snapshot)])
    first = verify_delivery(base).to_dict()["declared_policy"]
    second = verify_delivery({**base, "attestations": [_attestation(snapshot)]}).to_dict()["declared_policy"]
    assert first == second
    assert first["sha256"] == "sha256:" + hashlib.sha256(
        delivery.canonical_json(first["normalized"])).hexdigest()


@pytest.mark.parametrize("url,accepted", [("https://tracker.example/WI-2026-0001", True),
                                           ("http://tracker.example/WI-2026-0001", False)])
def test_identity_bound_work_item_requires_production_shape(monkeypatch, tmp_path, url, accepted):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    expected = {"work_item_id": "WI-2026-0001", "provider": "azure_devops", "provider_id": "42",
                "provider_url": url, "fingerprint": "sha256:" + "a" * 64,
                "operations": ["update_tasks"]}
    requirement = {"evidence": {"id": "work", "kind": "work-item", "description": "work item",
                                "required_before": "final"},
                   "freshness": {"mode": "identity_bound", "expected": expected}}
    attestation = {"id": "work", "kind": "work-item", "status": "pass", "source": url,
                   "observed_at": snapshot.captured_at.replace("+00:00", "Z"),
                   "provenance": {**expected, "attestor": "release-harness"}}

    result = verify_delivery(request(tmp_path, requirements=[requirement],
                                     attestations=[attestation])).to_dict()
    assert bool(result["evidence"]["harness_attested"]["accepted"]) is accepted
    assert result["requirement_checks"][0]["status"] == ("pass" if accepted else "fail")


def test_attestation_id_collision_cannot_enter_mechanical_lane(monkeypatch, tmp_path):
    _git(tmp_path)
    snapshot, _ = _captured(monkeypatch, tmp_path)
    requirement = _requirement(snapshot, kind="narrative")
    attestation = _attestation(snapshot, kind="narrative")
    attested_check = PRCheck("subjective.narrative", "pass", "narrative", ("tests.focused",), {})
    monkeypatch.setattr(delivery, "evaluate_pr_readiness",
                        lambda *_a, **_k: PRReadinessResult(True, (attested_check,)))

    result = verify_delivery(request(tmp_path, requirements=[requirement], attestations=[attestation])).to_dict()

    assert result["pr_readiness"]["checks"][0]["provenance_class"] == "harness_attested"
    assert "tests.focused" not in {entry["id"] for entry in result["evidence"]["mechanical"]}


def test_production_advisory_unknown_does_not_block(monkeypatch, tmp_path):
    _git(tmp_path)
    _captured(monkeypatch, tmp_path)
    advisory = PRCheck("subjective.readability", "unknown", "readability", (), {})
    monkeypatch.setattr(delivery, "evaluate_pr_readiness",
                        lambda *_a, **_k: PRReadinessResult(True, (advisory,)))

    result = verify_delivery(request(tmp_path)).to_dict()

    assert result["pr_readiness"]["checks"][0]["blocking"] is False
    assert result["eligible_under_declared_policy"] is True
