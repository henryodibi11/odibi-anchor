"""Real-Git integration for the managed local snapshot evidence provider."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from odibi_anchor._pr_readiness import PR_CONFIG_DEFAULTS, evaluate_pr_readiness
from odibi_anchor._repository_snapshot import validate_repository_snapshot
from odibi_anchor.operational import (
    CapabilityRequest,
    ContractError,
    capture_local_git_snapshot_evidence,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "module.py").write_text('"""Base."""\nVALUE = 1\n', encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "commit.gpgSign=false", "commit", "-m", "base")
    git(tmp_path, "switch", "-c", "feature")
    (tmp_path / "committed.py").write_text('"""Committed."""\nVALUE = 1\n', encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "commit.gpgSign=false", "commit", "-m", "feature")
    (tmp_path / "staged.py").write_text('"""Staged."""\nVALUE = 1\n', encoding="utf-8")
    git(tmp_path, "add", "staged.py")
    (tmp_path / "module.py").write_text('"""Base."""\nVALUE = 2\n', encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    return tmp_path


def request(root: Path, **constraints: int) -> CapabilityRequest:
    return CapabilityRequest(
        request_id="local-snapshot",
        capability_id="repository.local-change-snapshot",
        capability_contract_version="1.0",
        question="What exact local Git change state is under review?",
        target={"worktree": str(root), "configured_target_ref": "main"},
        scope={"worktree": str(root), "change_classes": ["committed", "staged", "unstaged", "untracked"]},
        required_coverage={"local_change_classes": "all"},
        freshness_requirement={"snapshot_bound": True},
        resource_constraints=constraints,
        pinned_provider_id="odibi-anchor.local-git-snapshot",
    )


def test_production_provider_reuses_snapshot_for_intake_readiness_and_staleness(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_globals = capture_local_git_snapshot_evidence.__globals__
    original_capture = provider_globals["capture_repository_snapshot"]
    captured = []

    def count_capture(*args, **kwargs):
        snapshot = original_capture(*args, **kwargs)
        captured.append(snapshot)
        return snapshot

    monkeypatch.setitem(provider_globals, "capture_repository_snapshot", count_capture)
    snapshot, intake = capture_local_git_snapshot_evidence(request(repository))
    assert snapshot is not None and captured == [snapshot] and snapshot is captured[0]
    value = intake.to_dict()

    assert value["attempt"]["acquisition_outcome"] == "succeeded"
    assert value["attempt"]["completeness"] == "complete"
    assert value["assessment"] == "unknown"
    assert value["attempt"]["environment_identity"]["remote_validated"] is False
    assert value["attempt"]["source_identity"]["head_sha"] == snapshot.head_sha
    assert value["attempt"]["source_identity"]["target_sha"] == snapshot.target_sha
    assert value["attempt"]["source_identity"]["merge_base_sha"] == snapshot.merge_base_sha
    coverage = value["attempt"]["coverage"]
    assert coverage["staged_paths"] == ["staged.py"]
    assert coverage["unstaged_paths"] == ["module.py"]
    assert coverage["untracked_paths"] == ["untracked.txt"]
    assert "committed.py" in coverage["changed_paths"]
    assert value["attempt"]["limits"]["commands"]
    assert value["claims"][0]["freshness_anchor"]["worktree_fingerprint"]

    readiness = evaluate_pr_readiness(
        snapshot, PR_CONFIG_DEFAULTS,
        intended_pr_paths=tuple(snapshot.changed_paths),
    )
    assert next(check for check in readiness.checks if check.id == "snapshot.fresh").status == "pass"

    original_freshness = value["claims"][0]["freshness_anchor"]
    (repository / "module.py").write_text('"""Base."""\nVALUE = 3\n', encoding="utf-8")
    assert "worktree" in validate_repository_snapshot(snapshot)[1]
    assert intake.to_dict()["claims"][0]["freshness_anchor"] == original_freshness


def test_missing_target_is_partial_without_fabricated_base(repository: Path) -> None:
    requested = request(repository)
    requested = CapabilityRequest(
        **{**requested.to_dict(), "target": {"worktree": str(repository), "configured_target_ref": "missing"}},
    )
    snapshot, intake = capture_local_git_snapshot_evidence(requested)
    assert snapshot is not None and snapshot.target_sha is None and snapshot.merge_base_sha is None
    assert intake.attempt.acquisition_outcome == "succeeded"
    assert intake.attempt.completeness == "partial"
    assert intake.assessment == "unknown"


def test_non_git_target_is_unavailable_without_claims(tmp_path: Path) -> None:
    snapshot, intake = capture_local_git_snapshot_evidence(request(tmp_path))
    assert snapshot is None
    assert intake.attempt.acquisition_outcome == "unavailable"
    assert intake.attempt.completeness == "not_applicable"
    assert intake.claims == ()


def test_denied_read_does_not_invoke_capture(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("capture must not run")

    monkeypatch.setitem(
        capture_local_git_snapshot_evidence.__globals__, "capture_repository_snapshot", forbidden,
    )
    snapshot, intake = capture_local_git_snapshot_evidence(request(repository), read_allowed=False)
    assert snapshot is None
    assert intake.attempt.acquisition_outcome == "denied"
    assert intake.claims == ()

    denied_request = CapabilityRequest(**{**request(repository).to_dict(), "allowed_effects": []})
    snapshot, intake = capture_local_git_snapshot_evidence(denied_request)
    assert snapshot is None and intake.attempt.acquisition_outcome == "denied"


def test_capture_error_after_eligibility_is_retained(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("capture failed")

    monkeypatch.setitem(
        capture_local_git_snapshot_evidence.__globals__, "capture_repository_snapshot", fail,
    )
    snapshot, intake = capture_local_git_snapshot_evidence(request(repository))
    assert snapshot is None
    assert intake.attempt.acquisition_outcome == "error"
    assert intake.attempt.completeness == "unknown"
    assert intake.attempt.error["type"] == "RuntimeError"

    monkeypatch.setitem(
        capture_local_git_snapshot_evidence.__globals__, "capture_repository_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("post-eligibility race")),
    )
    _, intake = capture_local_git_snapshot_evidence(request(repository))
    assert intake.attempt.acquisition_outcome == "error"
    assert intake.attempt.error["type"] == "ValueError"


def test_bounded_normalization_is_partial_and_discloses_counts(repository: Path) -> None:
    snapshot, intake = capture_local_git_snapshot_evidence(
        request(repository, max_paths=1, max_ranges=1, max_commands=1),
    )
    assert snapshot is not None
    assert intake.attempt.acquisition_outcome == "succeeded"
    assert intake.attempt.completeness == "partial"
    assert intake.attempt.truncated is True
    assert intake.attempt.coverage["path_counts"]["changed"] == len(snapshot.changed_paths)
    assert len(intake.attempt.coverage["changed_paths"]) == 1
    assert "commands" in intake.attempt.limits["truncated_fields"]


def test_byte_budget_and_managed_exclusions_are_disclosed(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_globals = capture_local_git_snapshot_evidence.__globals__
    original_capture = provider_globals["capture_repository_snapshot"]

    def large_snapshot(*args, **kwargs):
        snapshot = original_capture(*args, **kwargs)
        long_paths = tuple(f"directory/{index:04d}-{'x' * 3000}.py" for index in range(100))
        return replace(snapshot, changed_paths=long_paths)

    monkeypatch.setitem(provider_globals, "capture_repository_snapshot", large_snapshot)
    _, intake = capture_local_git_snapshot_evidence(request(repository))
    assert intake.attempt.completeness == "partial"
    assert "changed" in intake.attempt.limits["truncated_fields"]
    assert intake.attempt.limits["projection_bytes_used"] <= 262_144

    monkeypatch.setitem(provider_globals, "capture_repository_snapshot", original_capture)
    _, intake = capture_local_git_snapshot_evidence(request(repository), artifact_root=repository)
    assert intake.attempt.completeness == "partial"
    assert "problems" in intake.attempt.additional_exclusions
    assert {item["reason"] for item in intake.attempt.limits["managed_exclusions"]} == {
        "explicitly_classified",
    }


def test_oversized_attestation_is_rejected_before_capture(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("capture must not run")

    monkeypatch.setitem(
        capture_local_git_snapshot_evidence.__globals__, "capture_repository_snapshot", forbidden,
    )
    with pytest.raises(ContractError, match="caller_attestation exceeds"):
        capture_local_git_snapshot_evidence(
            request(repository), caller_attestation={"parts": ["x" * 12_000] * 3},
        )


def test_request_must_pin_exact_capability(repository: Path) -> None:
    invalid = CapabilityRequest(
        request_id="invalid",
        capability_id="repository.local-change-snapshot",
        capability_contract_version="1.0",
        question="snapshot",
        target={"worktree": str(repository), "configured_target_ref": "main"},
        scope={"worktree": str(repository)},
        required_coverage={"local": True},
        freshness_requirement={"snapshot_bound": True},
    )
    with pytest.raises(ContractError, match="pin"):
        capture_local_git_snapshot_evidence(invalid)

    unsupported = CapabilityRequest(**{
        **request(repository).to_dict(),
        "scope": {"worktree": str(repository), "change_classes": ["committed"]},
    })
    with pytest.raises(ContractError, match="all four local change classes"):
        capture_local_git_snapshot_evidence(unsupported)


def test_derived_identifier_bound_is_checked_before_capture(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = CapabilityRequest(**{**request(repository).to_dict(), "request_id": "x" * 16_384})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("capture must not run")

    monkeypatch.setitem(
        capture_local_git_snapshot_evidence.__globals__, "capture_repository_snapshot", forbidden,
    )
    with pytest.raises(ContractError, match="too long"):
        capture_local_git_snapshot_evidence(requested)
