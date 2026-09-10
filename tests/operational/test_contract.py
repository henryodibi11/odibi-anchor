import math
import subprocess

import pytest

from odibi_anchor.operational import (
    CapabilityRequest,
    CollectionAttempt,
    CollectorResult,
    ContractError,
    EvidenceClaim,
    EvidenceIntake,
    ProviderDeclaration,
    canonical_json,
    normalize_json,
    utc_now,
)

NOW = "2026-07-24T20:00:00Z"
LATER = "2026-07-24T20:01:00Z"


def request(*, pinned_provider_id=None, partial_evidence_useful=True):
    return CapabilityRequest(
        request_id="review-source",
        capability_id="repository.exact-search",
        capability_contract_version="1.0",
        question="Where is review evidence normalized?",
        target={"repository": "odibi-anchor", "commit": "abc123"},
        scope={"paths": ["src", "tests"]},
        required_coverage={"paths": "all"},
        freshness_requirement={"commit": "abc123", "dirty_worktree": False},
        exclusions=("vendor",),
        resource_constraints={"max_results": 100},
        partial_evidence_useful=partial_evidence_useful,
        pinned_provider_id=pinned_provider_id,
    )


def provider(*, provider_id="local-rg", execution_class="managed", coverage_semantics="exhaustive"):
    return ProviderDeclaration(
        provider_id=provider_id,
        implementation_version="14.1.0",
        capability_id="repository.exact-search",
        supported_contract_versions=("1.0",),
        execution_class=execution_class,
        coverage_semantics=coverage_semantics,
        maturity="fixture",
    )


def attempt(**overrides):
    values = {
        "attempt_id": "attempt-1",
        "request_id": "review-source",
        "provider_id": "local-rg",
        "provider_binding": "fixture_supplied",
        "provenance_kind": "mechanical",
        "acquisition_outcome": "succeeded",
        "completeness": "complete",
        "started_at": NOW,
        "completed_at": LATER,
        "executor_identity": {"runtime": "odibi-anchor"},
        "coverage": {"paths_examined": ["src", "tests"]},
        "source_identity": {"repository": "odibi-anchor", "commit": "abc123"},
        "environment_identity": {"platform": "debian-12"},
        "raw_artifact_ref": {"path": "evidence/search.json", "sha256": "def456"},
    }
    values.update(overrides)
    return CollectionAttempt(**values)


def claim(**overrides):
    values = {
        "claim_id": "claim-1",
        "attempt_id": "attempt-1",
        "statement": "Operational contracts normalize review evidence.",
        "polarity": "presence",
        "epistemic_class": "direct_observation",
        "source_locator": {"path": "src/odibi_anchor/operational/_contract.py", "line": 1},
        "coverage_basis": {"matched_files": 1, "searched_paths": ["src", "tests"]},
        "freshness_anchor": {"commit": "abc123", "dirty_worktree": False},
        "provider_metadata": {"local-rg": {"pattern": "evidence", "rank": 1}},
    }
    values.update(overrides)
    return EvidenceClaim(**values)


def test_collector_result_is_strict_versioned_utc_contract():
    result = CollectorResult("environment", "collected", facts={"items": (1, True, None)})
    value = result.to_dict()
    assert value["schema_version"] == 1
    assert value["status"] == "collected"
    assert value["observed_at"].endswith("Z")
    assert canonical_json(value) == canonical_json(value)


def test_collector_result_shape_remains_backward_compatible():
    assert set(CollectorResult("environment", "collected").to_dict()) == {
        "collector", "status", "source", "environment", "facts", "findings",
        "limitations", "error", "redaction", "observed_at", "schema_version",
    }


@pytest.mark.parametrize("value", [math.nan, math.inf, object(), {1: "value"}])
def test_normalization_rejects_non_json_values_without_repr(value):
    with pytest.raises(ContractError):
        normalize_json(value)


def test_non_collected_result_cannot_claim_findings():
    with pytest.raises(ContractError):
        CollectorResult("environment", "denied", findings=({"statement": "found"},))


def test_managed_intake_preserves_provider_neutral_evidence_dimensions():
    intake = EvidenceIntake(
        request=request(),
        provider=provider(),
        attempt=attempt(),
        claims=(claim(),),
        assessment="pass",
        assessment_basis=("claim-1",),
    )

    value = intake.to_dict()

    assert value["attempt"]["acquisition_outcome"] == "succeeded"
    assert value["attempt"]["completeness"] == "complete"
    assert value["assessment"] == "pass"
    assert value["attempt"]["raw_artifact_ref"]["sha256"] == "def456"
    assert value["claims"][0]["freshness_anchor"] == {"commit": "abc123", "dirty_worktree": False}
    assert value["claims"][0]["provider_metadata"]["local-rg"]["pattern"] == "evidence"
    assert canonical_json(value) == canonical_json(value)


def test_real_git_snapshot_flows_through_managed_intake(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet")
    tracked = tmp_path / "review.txt"
    tracked.write_text("provider-neutral evidence\n", encoding="utf-8")
    git("add", "review.txt")
    git(
        "-c", "commit.gpgSign=false",
        "-c", "user.name=Odibi Anchor Test",
        "-c", "user.email=anchor-test@example.invalid",
        "commit", "--quiet", "-m", "seed evidence",
    )
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    tracked_files = git("ls-files").splitlines()
    git_version = subprocess.run(
        ["git", "--version"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    observed_at = utc_now()

    intake = EvidenceIntake(
        request=CapabilityRequest(
            request_id="git-snapshot",
            capability_id="repository.snapshot",
            capability_contract_version="1.0",
            question="What exact repository snapshot was reviewed?",
            target={"repository_path": str(tmp_path)},
            scope={"paths": tracked_files},
            required_coverage={"tracked_files": "all"},
            freshness_requirement={"commit": commit, "dirty_worktree": False},
            pinned_provider_id="git-cli",
        ),
        provider=ProviderDeclaration(
            provider_id="git-cli",
            implementation_version=git_version,
            capability_id="repository.snapshot",
            supported_contract_versions=("1.0",),
            execution_class="managed",
            coverage_semantics="exhaustive",
            maturity="integration-tested",
        ),
        attempt=CollectionAttempt(
            attempt_id="git-snapshot-attempt",
            request_id="git-snapshot",
            provider_id="git-cli",
            provider_binding="caller_pinned",
            provenance_kind="mechanical",
            acquisition_outcome="succeeded",
            completeness="complete",
            started_at=observed_at,
            completed_at=utc_now(),
            executor_identity={"executable": "git"},
            coverage={"tracked_files": tracked_files},
            source_identity={"repository_path": str(tmp_path), "commit": commit},
            environment_identity={"git_version": git_version},
        ),
        claims=(EvidenceClaim(
            claim_id="git-snapshot-claim",
            attempt_id="git-snapshot-attempt",
            statement="The reviewed repository was clean at the captured commit.",
            polarity="presence",
            epistemic_class="direct_observation",
            source_locator={"repository_path": str(tmp_path), "commit": commit},
            coverage_basis={"tracked_files": tracked_files, "status_porcelain": status},
            freshness_anchor={"commit": commit, "dirty_worktree": bool(status)},
            provider_metadata={"git-cli": {"version": git_version}},
        ),),
        assessment="pass",
        assessment_basis=("git-snapshot-claim",),
    ).to_dict()

    tracked.write_text("worktree changed after capture\n", encoding="utf-8")

    assert git("status", "--porcelain") == "M review.txt"
    assert intake["attempt"]["source_identity"]["commit"] == commit
    assert intake["claims"][0]["freshness_anchor"] == {
        "commit": commit,
        "dirty_worktree": False,
    }
    assert intake["claims"][0]["coverage_basis"]["tracked_files"] == ["review.txt"]


def test_delegated_intake_preserves_harness_attestation_separately_from_claim_class():
    shared_request = request()
    delegated_attempt = attempt(
        provider_id="harness-search",
        provenance_kind="attested",
        executor_identity={"harness": "generic-agent", "session": "fixture-7"},
    )
    delegated_claim = claim(
        epistemic_class="direct_observation",
        provider_metadata={"harness-search": {"native_result_type": "search-result"}},
    )

    value = EvidenceIntake(
        request=shared_request,
        provider=provider(provider_id="harness-search", execution_class="delegated"),
        attempt=delegated_attempt,
        claims=(delegated_claim,),
        assessment="pass",
        assessment_basis=("claim-1",),
    ).to_dict()

    assert value["attempt"]["provenance_kind"] == "attested"
    assert value["attempt"]["executor_identity"]["harness"] == "generic-agent"
    assert value["claims"][0]["epistemic_class"] == "direct_observation"
    assert "amp" not in canonical_json(value).decode().lower()

    managed = EvidenceIntake(
        request=shared_request,
        provider=provider(),
        attempt=attempt(),
        assessment="unknown",
    )
    assert managed.request is shared_request


def test_unavailable_intake_retains_limit_without_fabricating_fallback_or_assessment():
    unavailable = EvidenceIntake(
        request=request(),
        attempt=attempt(
            provider_id=None,
            provider_binding="not_applicable",
            provenance_kind="attested",
            acquisition_outcome="unavailable",
            completeness="not_applicable",
            raw_artifact_ref=None,
            limitations=("No eligible provider was supplied",),
        ),
        assessment="unknown",
    ).to_dict()

    assert unavailable["provider"] is None
    assert unavailable["claims"] == []
    assert unavailable["assessment_basis"] == []
    assert unavailable["attempt"]["limitations"] == ["No eligible provider was supplied"]


def test_partial_success_remains_independent_from_pass_assessment():
    partial = EvidenceIntake(
        request=request(partial_evidence_useful=False),
        provider=provider(coverage_semantics="bounded"),
        attempt=attempt(
            completeness="partial",
            truncated=True,
            additional_exclusions=("generated",),
            limitations=("Result limit reached",),
        ),
        claims=(claim(uncertainty=("Generated sources were excluded",)),),
        assessment="pass",
        assessment_basis=("claim-1",),
    ).to_dict()

    assert partial["attempt"]["acquisition_outcome"] == "succeeded"
    assert partial["attempt"]["completeness"] == "partial"
    assert partial["assessment"] == "pass"
    assert partial["request"]["partial_evidence_useful"] is False


def test_denied_provider_retains_disallowed_effect_without_fallback():
    denied = EvidenceIntake(
        request=request(),
        provider=ProviderDeclaration(
            provider_id="write-search",
            implementation_version="1",
            capability_id="repository.exact-search",
            supported_contract_versions=("1.0",),
            execution_class="managed",
            required_effects=("external_mutation",),
        ),
        attempt=attempt(
            provider_id="write-search",
            acquisition_outcome="denied",
            completeness="not_applicable",
            raw_artifact_ref=None,
            limitations=("The request permits reads only",),
        ),
        assessment="unknown",
    ).to_dict()

    assert denied["attempt"]["acquisition_outcome"] == "denied"
    assert denied["provider"]["required_effects"] == ["external_mutation"]
    assert denied["claims"] == []


def test_partial_error_may_retain_claims_but_requires_provider_and_applicable_completeness():
    retained = EvidenceIntake(
        request=request(),
        provider=provider(coverage_semantics="bounded"),
        attempt=attempt(
            acquisition_outcome="error",
            completeness="partial",
            limitations=("Search process exited after producing one match",),
            error={"type": "ProcessError", "message": "exit 1"},
        ),
        claims=(claim(uncertainty=("Later matches may be missing",)),),
        assessment="unknown",
    ).to_dict()
    assert retained["attempt"]["acquisition_outcome"] == "error"
    assert len(retained["claims"]) == 1

    with pytest.raises(ContractError, match="provider declaration"):
        EvidenceIntake(
            request=request(),
            attempt=attempt(
                provider_id=None,
                provider_binding="not_applicable",
                provenance_kind="attested",
                acquisition_outcome="error",
                completeness="partial",
                raw_artifact_ref=None,
            ),
            claims=(claim(),),
            assessment="unknown",
        )
    with pytest.raises(ContractError, match="applicable completeness"):
        attempt(acquisition_outcome="error", completeness="not_applicable")


def test_contracts_detach_and_freeze_caller_owned_json():
    target = {"repository": {"name": "odibi-anchor"}}
    value = request().to_dict()
    detached = CapabilityRequest(
        request_id="r",
        capability_id="c",
        capability_contract_version="1",
        question="q",
        target=target,
        scope={"paths": ["src"]},
        required_coverage={"paths": "all"},
        freshness_requirement={"commit": "abc"},
    )
    target["repository"]["name"] = "mutated"

    assert detached.to_dict()["target"]["repository"]["name"] == "odibi-anchor"
    with pytest.raises(TypeError):
        detached.target["new"] = "value"
    with pytest.raises(TypeError):
        detached.target["repository"]["name"] = "value"
    with pytest.raises(AttributeError):
        detached.scope["paths"].append("tests")
    assert value["target"]["repository"] == "odibi-anchor"


@pytest.mark.parametrize(
    "bad_attempt",
    [
        {"acquisition_outcome": "succeeded", "completeness": "not_applicable"},
        {"acquisition_outcome": "denied", "completeness": "partial"},
        {"completeness": "complete", "truncated": True, "limitations": ("cut off",)},
        {"started_at": LATER, "completed_at": NOW},
    ],
)
def test_attempt_rejects_collapsed_or_inconsistent_dimensions(bad_attempt):
    with pytest.raises(ContractError):
        attempt(**bad_attempt)


def test_intake_rejects_mismatched_relationships_and_unsupported_effects():
    with pytest.raises(ContractError, match="request_id"):
        EvidenceIntake(request=request(), provider=provider(), attempt=attempt(request_id="other"), assessment="unknown")
    with pytest.raises(ContractError, match="effects"):
        EvidenceIntake(
            request=request(),
            provider=ProviderDeclaration(
                provider_id="writer",
                implementation_version="1",
                capability_id="repository.exact-search",
                supported_contract_versions=("1.0",),
                execution_class="managed",
                required_effects=("external_mutation",),
            ),
            attempt=attempt(provider_id="writer"),
            assessment="unknown",
        )


def test_absence_claim_requires_exhaustive_complete_coverage():
    absence = claim(polarity="absence", statement="No matching implementation exists.")
    with pytest.raises(ContractError, match="absence claims"):
        EvidenceIntake(
            request=request(),
            provider=provider(coverage_semantics="best_effort"),
            attempt=attempt(),
            claims=(absence,),
            assessment="fail",
            assessment_basis=("claim-1",),
        )


def test_inference_requires_derivation_and_assessment_requires_claim_basis():
    with pytest.raises(ContractError, match="derivation"):
        claim(epistemic_class="inference")
    with pytest.raises(ContractError, match="claim evidence"):
        EvidenceIntake(request=request(), provider=provider(), attempt=attempt(), assessment="pass")


def test_delegated_execution_requires_an_attesting_executor():
    with pytest.raises(ContractError, match="executor_identity"):
        EvidenceIntake(
            request=request(),
            provider=provider(provider_id="harness-search", execution_class="delegated"),
            attempt=attempt(
                provider_id="harness-search",
                provenance_kind="attested",
                executor_identity={},
            ),
            assessment="unknown",
        )


@pytest.mark.parametrize("schema_version", [True, 1.0, 2])
def test_intake_schema_version_requires_the_supported_integer(schema_version):
    with pytest.raises(ContractError, match="schema_version"):
        EvidenceIntake(
            request=request(),
            provider=provider(),
            attempt=attempt(),
            assessment="unknown",
            schema_version=schema_version,
        )


def test_intake_enforces_the_canonical_payload_bound():
    oversized_metadata = {f"field-{index}": "x" * 16_384 for index in range(65)}
    with pytest.raises(ContractError, match="payload exceeds"):
        EvidenceIntake(
            request=request(),
            provider=provider(),
            attempt=attempt(),
            claims=(claim(provider_metadata=oversized_metadata),),
            assessment="unknown",
        )


def test_pinned_provider_must_be_honored_without_resolution():
    pinned = request(pinned_provider_id="local-rg")
    value = EvidenceIntake(
        request=pinned,
        provider=provider(),
        attempt=attempt(provider_binding="caller_pinned"),
        assessment="unknown",
    ).to_dict()
    assert value["request"]["pinned_provider_id"] == "local-rg"

    with pytest.raises(ContractError, match="pinned provider"):
        EvidenceIntake(
            request=pinned,
            provider=provider(provider_id="other"),
            attempt=attempt(provider_id="other"),
            assessment="unknown",
        )
