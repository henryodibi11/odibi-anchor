"""Per-control promotion, exception, sparse-evidence, and rollback tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from odibi_anchor.assurance import (
    GovernedException,
    OutcomeComparison,
    PromotionPacket,
    PromotionPolicy,
    assess_promotion,
    rollback_triggers,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "src/odibi_anchor/assurance/resources/promotion_policy.json"


def _comparison() -> OutcomeComparison:
    cohort = {"control_version": "Anchor-T1-SCOPE-INTEGRITY@1.0"}
    return OutcomeComparison(cohort, cohort, {"first_pass_success": 0.1}, (), (), True)


def _packet(policy: PromotionPolicy, **changes) -> PromotionPacket:
    values = {
        "packet_id": "synthetic-packet",
        "assessed_at": "2026-08-21T00:00:00Z",
        "control_version": "Anchor-T1-SCOPE-INTEGRITY@1.0",
        "policy_digest": policy.policy_digest,
        "current_mode": "shadow",
        "requested_mode": "warn",
        "comparison": _comparison(),
        "complete_matrix": True,
        "sealed_holdout": True,
        "dogfood_tasks": 5,
        "applicable_real_tasks": 5,
        "elapsed_days": 0,
        "tiers": ("T1", "T2"),
        "scopes": ("localized", "systemic"),
        "agent_families": ("family-a", "family-b"),
        "independent_agents": 2,
        "hosts": ("direct", "mcp"),
        "approvals": (
            {
                "approval_id": "a",
                "reviewer_id": "r1",
                "role": "assurance_reviewer",
                "implemented_control": "false",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            {
                "approval_id": "b",
                "reviewer_id": "r2",
                "role": "maintainer",
                "implemented_control": "false",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        ),
        "remediation_proven": False,
        "rollback_demonstrated": False,
        "critical_escaped_defects": 0,
        "critical_false_positives": 0,
        "unavailable_treated_as_pass": 0,
        "evaluator_leakage": False,
        "open_outcome_regression": False,
        "ceremony_within_budget": True,
        "synthetic": False,
    }
    values.update(changes)
    return PromotionPacket(**values)


def test_policy_is_canonical_digest_bound_and_exact_versioned() -> None:
    policy = PromotionPolicy.from_path(POLICY_PATH)
    assert assess_promotion(_packet(policy), policy).disposition == "eligible"
    unknown = _packet(policy, control_version="Anchor-T1-SCOPE-INTEGRITY@2.0")
    assert assess_promotion(unknown, policy).disposition == "invalid"


@pytest.mark.parametrize(
    ("change", "criterion"),
    [
        ({"complete_matrix": False}, "matrix:incomplete"),
        ({"sealed_holdout": False}, "holdout:missing"),
        (
            {"comparison": OutcomeComparison(
                {"control_version": "Anchor-T1-TESTS@1.0"},
                {"control_version": "Anchor-T1-TESTS@1.0"},
                {"first_pass_success": 0.1}, (), (), True,
            )},
            "comparison:control_version_mismatch",
        ),
        ({"dogfood_tasks": 4}, "dogfood:insufficient"),
        ({"scopes": ("localized",)}, "scope_coverage:incomplete"),
        ({"agent_families": ("family-a",), "independent_agents": 3}, "agent_coverage:insufficient"),
        ({"hosts": ("direct",)}, "host_coverage:insufficient"),
        ({"synthetic": True}, "evidence:synthetic_infrastructure_only"),
    ],
)
def test_each_criterion_holds_without_aggregate_shortcut(change, criterion: str) -> None:
    policy = PromotionPolicy.from_path(POLICY_PATH)
    decision = assess_promotion(_packet(policy, **change), policy)
    assert decision.disposition == "hold"
    assert criterion in decision.failed_criteria


def test_transition_is_sequential_and_sparse_block_window_holds() -> None:
    policy = PromotionPolicy.from_path(POLICY_PATH)
    skipped = _packet(policy, requested_mode="block")
    assert assess_promotion(skipped, policy).disposition == "invalid"
    warn_policy = PromotionPolicy.create(
        policy.policy_version,
        {
            "Anchor-T1-SCOPE-INTEGRITY@1.0": {
                **dict(policy.controls["Anchor-T1-SCOPE-INTEGRITY@1.0"]),
                "current_mode": "warn",
            },
        },
    )
    sparse = _packet(
        warn_policy,
        current_mode="warn",
        requested_mode="block",
        applicable_real_tasks=19,
        elapsed_days=13,
        remediation_proven=True,
        rollback_demonstrated=True,
    )
    decision = assess_promotion(sparse, warn_policy)
    assert decision.disposition == "hold"
    assert {"real_tasks:insufficient", "elapsed_window:insufficient"}.issubset(decision.failed_criteria)


def test_governed_exception_is_exact_independent_and_maximum_thirty_days() -> None:
    exception = GovernedException(
        "ex-1",
        "Anchor-T1-SCOPE-INTEGRITY@1.0",
        "shadow",
        "repo",
        "task",
        "subject",
        "temporary provider outage",
        "owner",
        "manual review",
        "requester",
        "approver",
        "https://example.invalid/issue/1",
        "2026-08-01T00:00:00Z",
        "2026-08-31T00:00:00Z",
        0,
        "signed-digest",
    )
    assert (
        exception.validate(
            now="2026-08-20T00:00:00Z",
            control_version=exception.control_version,
            mode="shadow",
        )
        == ()
    )
    invalid = replace(exception, approver="requester", expires_at="2026-09-01T00:00:00Z")
    failures = invalid.validate(
        now="2026-08-20T00:00:00Z",
        control_version=exception.control_version,
        mode="shadow",
    )
    assert "exception:approval_not_independent" in failures
    assert "exception:lifetime_exceeds_30_days" in failures


@pytest.mark.parametrize("trigger", sorted(rollback_triggers()))
def test_every_automatic_trigger_rolls_block_back_to_warn(trigger: str) -> None:
    source = PromotionPolicy.from_path(POLICY_PATH)
    control = {**dict(source.controls["Anchor-T1-SCOPE-INTEGRITY@1.0"]), "current_mode": "block"}
    policy = PromotionPolicy.create(
        source.policy_version,
        {
            "Anchor-T1-SCOPE-INTEGRITY@1.0": control,
        },
    )
    changes = {"current_mode": "block", "requested_mode": "block"}
    changes.update(
        {
            "policy_digest_mismatch": {"policy_digest": "sha256:wrong"},
            "evaluator_leakage": {"evaluator_leakage": True},
            "critical_false_positive": {"critical_false_positives": 1},
            "critical_escaped_defect": {"critical_escaped_defects": 1},
            "unavailable_treated_as_pass": {"unavailable_treated_as_pass": 1},
            "expired_approval": {"approval_expired": True},
        }[trigger]
    )
    decision = assess_promotion(_packet(policy, **changes), policy)
    assert decision.disposition == "rollback"
    assert decision.resulting_mode == "warn"
    assert decision.retained_history is True
