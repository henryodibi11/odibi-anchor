"""Public corpus coverage, independence, isolation, and harness-status tests."""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

from odibi_anchor.assurance import CONTROL_CATALOG, load_scenario_manifest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "src/odibi_anchor/assurance/resources/scenario_manifest.json"


def test_manifest_has_two_materially_distinct_scenarios_in_every_cartesian_cell() -> None:
    scenarios = load_scenario_manifest(MANIFEST)
    cells = Counter((item.tier, item.scope) for item in scenarios)
    assert cells == {(tier, scope): 2 for tier in ("T0", "T1", "T2", "T3") for scope in ("localized", "systemic")}
    assert len({item.scenario_family for item in scenarios}) == len(scenarios)


def test_tier_and_scope_are_independent_explicit_dimensions() -> None:
    scenarios = load_scenario_manifest(MANIFEST)
    scopes_by_tier = {
        tier: {item.scope for item in scenarios if item.tier == tier} for tier in ("T0", "T1", "T2", "T3")
    }
    tiers_by_scope = {
        scope: {item.tier for item in scenarios if item.scope == scope} for scope in ("localized", "systemic")
    }
    assert all(scopes == {"localized", "systemic"} for scopes in scopes_by_tier.values())
    assert all(tiers == {"T0", "T1", "T2", "T3"} for tiers in tiers_by_scope.values())


def test_public_manifest_covers_required_families_without_evaluator_payload() -> None:
    scenarios = load_scenario_manifest(MANIFEST)
    known_controls = {
        f"{control.control_id}@{getattr(control, 'version', '1.0')}"
        for control in CONTROL_CATALOG
    }
    assert all(
        control in known_controls
        for scenario in scenarios
        for control in scenario.applicable_controls
    )
    traits = {trait for item in scenarios for trait in item.traits}
    assert {
        "negative",
        "not-applicable",
        "unavailable-provider",
        "malformed-evidence",
        "governed-exception",
        "rollback",
        "metamorphic",
        "adversarial-near-miss",
        "e6-lifecycle",
    }.issubset(traits)
    text = MANIFEST.read_text(encoding="utf-8")
    for forbidden in (
        "expected_answer",
        "seeded_defect_location",
        "reviewer_rubric",
        "promotion_threshold",
        "evaluator_payload",
    ):
        assert forbidden not in text


def test_splits_are_family_disjoint_and_each_tier_has_unsupported_invocation() -> None:
    scenarios = load_scenario_manifest(MANIFEST)
    assert len({(item.scenario_family, item.corpus_split) for item in scenarios}) == len(scenarios)
    for tier in ("T0", "T1", "T2", "T3"):
        assert any(item.tier == tier and "unsupported-invocation" in item.traits for item in scenarios)
    for scope in ("localized", "systemic"):
        assert any(item.scope == scope and "unavailable-provider" in item.traits for item in scenarios)


def test_missing_external_evaluators_are_unavailable_not_qualified(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "odibi_anchor.assurance.runner",
            "validate",
            "--manifest",
            str(MANIFEST),
            "--evaluator-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 3
    assert '"status": "unavailable"' in result.stderr
