"""Overlay evidence-state and exception semantics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from odibi_anchor.assurance import (
    OVERLAY_CONTROLS,
    OVERLAYS,
    CommandResultEvidence,
    EvidenceState,
    OverlayException,
    OverlayInput,
    evaluate_overlay_controls,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
SUBJECT = "sha256:" + "1" * 64
SCOPE = "sha256:" + "2" * 64


def _input_for(overlay_id: str) -> OverlayInput:
    overlay = next(item for item in OVERLAYS if item.overlay_id == overlay_id)
    predicate = overlay.predicate
    domains = frozenset({predicate.domains_any[0]}) if predicate.domains_any else frozenset()
    traits = frozenset({predicate.traits_any[0]}) if predicate.traits_any else frozenset()
    if predicate.require_domain_match:
        domains = frozenset({predicate.domains_any[0]})
        traits = frozenset({predicate.traits_any[0]})
    return OverlayInput(
        "change", "source_change", predicate.require_tier or "T2",
        domains=domains, traits=traits,
    )


def _evidence(kind: str, *, state: EvidenceState = "satisfied", passed: bool | None = True,
              evidence_id: str | None = None, subject: str = SUBJECT, scope: str = SCOPE,
              completed: str = "2026-08-20T11:00:00+00:00") -> CommandResultEvidence:
    return CommandResultEvidence(
        evidence_id or f"evidence:{kind}", kind, state,
        "2026-08-20T10:59:00+00:00", completed, subject, scope,
        "test-producer", "1", {"passed": passed}, {"limitations": []},
    )


@pytest.mark.parametrize("control", OVERLAY_CONTROLS, ids=lambda item: item.control_id)
def test_every_control_exposes_all_six_states(control):
    value = _input_for(control.overlay_id)
    evidence = tuple(_evidence(kind) for kind in control.required_evidence)
    _, satisfied = evaluate_overlay_controls(
        value, evidence, subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    result = next(item for item in satisfied if item.control_id == control.control_id)
    assert result.evidence_state == "satisfied"

    failed_evidence = (replace(evidence[0], state="failed", result={"passed": False}), *evidence[1:])
    _, failed = evaluate_overlay_controls(
        value, failed_evidence, subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    assert next(item for item in failed if item.control_id == control.control_id).evidence_state == "failed"

    unavailable_evidence = (replace(evidence[0], scope_digest="sha256:" + "3" * 64), *evidence[1:])
    _, unavailable = evaluate_overlay_controls(
        value, unavailable_evidence, subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    assert next(item for item in unavailable if item.control_id == control.control_id).evidence_state == "unavailable"

    _, not_assessed = evaluate_overlay_controls(
        value, (), subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    assert next(item for item in not_assessed if item.control_id == control.control_id).evidence_state == "not_assessed"

    _, not_applicable = evaluate_overlay_controls(
        OverlayInput("change", "source_change", "T2"), (), now=NOW,
    )
    assert next(item for item in not_applicable if item.control_id == control.control_id).evidence_state == "not_applicable"

    exception = OverlayException(
        control.control_id, control.version, "reviewed bounded exception", "owner",
        SCOPE, "2026-08-20T09:00:00+00:00", "2026-08-21T12:00:00+00:00",
        (evidence[0].evidence_id,),
    )
    _, excepted = evaluate_overlay_controls(
        value, evidence, (exception,), subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    assert next(item for item in excepted if item.control_id == control.control_id).evidence_state == "excepted"


def test_invocation_only_stale_wrong_scope_and_unexecuted_results_never_satisfy():
    control = next(item for item in OVERLAY_CONTROLS if item.control_id == "OPS-RUN-01")
    value = _input_for(control.overlay_id)
    evidence = (
        _evidence(control.required_evidence[0], passed=True),
        _evidence(control.required_evidence[1], passed=None),
    )
    _, results = evaluate_overlay_controls(
        value, evidence, subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    assert next(item for item in results if item.control_id == control.control_id).evidence_state == "unavailable"


def test_one_failed_control_remains_visible_when_all_other_evidence_passes():
    value = _input_for("security.application")
    controls = [item for item in OVERLAY_CONTROLS if item.overlay_id == "security.application"]
    kinds = sorted({kind for control in controls for kind in control.required_evidence})
    evidence = [_evidence(kind) for kind in kinds]
    evidence[0] = replace(evidence[0], state="failed", result={"passed": False})
    _, results = evaluate_overlay_controls(
        value, evidence, subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW,
    )
    applicable = [item for item in results if item.overlay_id == "security.application"]
    assert any(item.evidence_state == "failed" for item in applicable)
    assert all(item.disposition == "advisory" for item in applicable)


def test_exceptions_require_complete_current_scope_bound_metadata():
    control = OVERLAY_CONTROLS[0]
    with pytest.raises(ValueError, match="rationale and owner"):
        OverlayException(
            control.control_id, control.version, "", "", SCOPE, "2026-08-20T09:00:00+00:00",
            "2026-08-21T12:00:00+00:00", ("evidence",),
        )
    with pytest.raises(ValueError, match="compensating evidence"):
        OverlayException(
            control.control_id, control.version, "reason", "owner", SCOPE,
            "2026-08-20T09:00:00+00:00", "2026-08-21T12:00:00+00:00", (),
        )
