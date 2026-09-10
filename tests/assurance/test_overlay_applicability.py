"""Deterministic structured overlay selection tests."""

from __future__ import annotations

from typing import Any

import pytest

from odibi_anchor.assurance import (
    OVERLAYS,
    OverlayInput,
    OverlaySelectionItem,
    select_overlays,
)


def _input(**overrides: Any) -> OverlayInput:
    values: dict[str, Any] = {
        "work_type": "change",
        "execution_mode": "source_change",
        "assurance_tier": "T2",
        "domains": frozenset(),
        "traits": frozenset(),
        "declared_effects": frozenset(),
        "quality_attributes": frozenset(),
        "changed_path_classes": frozenset(),
        "repository_capabilities": frozenset(),
    }
    values.update(overrides)
    return OverlayInput(**values)


def _selected(value: OverlayInput) -> dict[str, OverlaySelectionItem]:
    return {item.overlay_id: item for item in select_overlays(value).selected}


@pytest.mark.parametrize("overlay", OVERLAYS, ids=lambda item: item.overlay_id)
def test_every_positive_predicate_atom_selects_its_overlay_with_exact_reason(overlay):
    predicate = overlay.predicate
    fields = (
        ("domains", predicate.domains_any, "domain"),
        ("traits", predicate.traits_any, "trait"),
        ("declared_effects", predicate.effects_any, "effect"),
        ("changed_path_classes", predicate.path_classes_any, "path_class"),
    )
    for field, atoms, prefix in fields:
        for atom in atoms:
            values: dict[str, Any] = {field: frozenset({atom})}
            if predicate.require_domain_match and field != "domains":
                values["domains"] = frozenset({predicate.domains_any[0]})
            if predicate.require_domain_match and field == "domains":
                values["traits"] = frozenset({predicate.traits_any[0]})
            if predicate.require_tier:
                values["assurance_tier"] = "T3"
            selected = _selected(_input(**values))
            assert overlay.overlay_id in selected
            reasons = selected[overlay.overlay_id].reason_codes
            assert f"{prefix}:{atom}" in reasons
            if predicate.require_tier:
                assert "tier:T3" in reasons


def test_required_conjunctions_fail_closed():
    assert "accessibility.user-interface" not in _selected(
        _input(domains=frozenset({"frontend"}))
    )
    assert "accessibility.user-interface" not in _selected(
        _input(traits=frozenset({"user_facing_interaction"}))
    )
    consequence = frozenset({"irreversible_change"})
    assert "change-safety.high-risk" not in _selected(_input(traits=consequence))
    assert "change-safety.high-risk" not in _selected(_input(assurance_tier="T3"))


def test_unrelated_negative_fixtures_select_zero_overlays():
    fixtures = (
        _input(),  # README typo, unit-test refactor, arithmetic helper, or planning prose
        _input(assurance_tier="T2"),  # reversible configuration edit
        _input(quality_attributes=frozenset({"reliability", "security"})),
    )
    for value in fixtures:
        selection = select_overlays(value)
        assert selection.selected == ()
        assert len(selection.not_selected) == 8
        assert {item.reason_codes for item in selection.not_selected} == {("predicate_not_met",)}


def test_collision_fixtures_union_controls_without_duplicates():
    ai_api = select_overlays(_input(
        domains=frozenset({"ai", "api", "data"}),
        traits=frozenset({"handles_personal_data"}),
    ))
    assert {item.overlay_id for item in ai_api.selected} == {
        "ai.agent-risk", "data.quality", "security.application",
    }
    controls = [control for item in ai_api.selected for control in item.control_ids]
    assert len(controls) == len(set(controls))

    destructive = select_overlays(_input(
        assurance_tier="T3",
        traits=frozenset({"schema_change", "production_runtime", "irreversible_change"}),
        declared_effects=frozenset({"schema_mutation"}),
    ))
    assert {item.overlay_id for item in destructive.selected} == {
        "architecture.change", "change-safety.high-risk", "data.quality",
        "operations.reliability",
    }


def test_unknown_enum_values_and_prose_fields_fail_closed():
    with pytest.raises(ValueError, match="unsupported domains"):
        _input(domains=frozenset({"generic prose about auth"}))
    payload = {
        "work_type": "change", "execution_mode": "source_change", "assurance_tier": "T2",
        "domains": [], "traits": [], "declared_effects": [], "quality_attributes": [],
        "changed_path_classes": [], "repository_capabilities": [], "description": "auth",
    }
    with pytest.raises(ValueError, match="exact structured fields"):
        OverlayInput.from_dict(payload)
