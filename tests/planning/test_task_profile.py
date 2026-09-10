"""Focused contract tests for canonical task-profile normalization."""

import json

import pytest

from odibi_anchor.planning._task_profile import EvidenceRequest, TaskProfile, normalize_task_profile


LEGACY_ROWS = [
    ("planning", "design", "artifact_only", "medium", "compact", ("general",), {"planning"}),
    ("documentation", "communicate", "artifact_only", "low", "direct", ("general",), {"documentation"}),
    ("implementation", "change", "source_change", "medium", "compact", ("code",), {"implementation"}),
    ("testing", "verify", "read_only", "medium", "compact", ("code",), {"testing"}),
    ("debugging", "investigate", "read_only", "medium", "compact", ("code",), {"debugging"}),
    ("review", "verify", "read_only", "medium", "direct", ("code",), {"review"}),
    ("migration", "change", "source_change", "high", "compact", ("code",), {"migration", "rollback"}),
    ("handoff", "communicate", "artifact_only", "low", "direct", ("general",), {"handoff"}),
    ("decision", "decide", "artifact_only", "medium", "compact", ("general",), {"alternatives"}),
    ("analysis", "investigate", "read_only", "medium", "compact", ("general",), {"analysis"}),
    ("retrospective", "communicate", "artifact_only", "low", "direct", ("general",), {"retrospective"}),
    ("greenfield", "design", "artifact_only", "high", "compact", ("code",), {"greenfield", "public-surface"}),
    ("spec_creation", "design", "artifact_only", "medium", "compact", ("code",), {"specification"}),
    ("data", "change", "data_change", "high", "compact", ("data",), {"data-write"}),
    ("etl", "operate", "data_change", "medium", "compact", ("data", "pipeline"), {"etl"}),
    ("reconciliation", "investigate", "read_only", "medium", "compact", ("data",), {"reconciliation"}),
    ("refresh", "operate", "data_change", "medium", "compact", ("data", "pipeline"), {"refresh"}),
]


@pytest.mark.parametrize("mode,work,effect,risk,rigor,domains,traits", LEGACY_ROWS)
def test_all_legacy_rows(mode, work, effect, risk, rigor, domains, traits):
    """Every approved legacy mode maps exactly to the compatibility table."""
    profile = normalize_task_profile(legacy_mode=mode)
    assert (profile.work_type, profile.execution_mode, profile.risk, profile.rigor) == (work, effect, risk, rigor)
    assert profile.domains == domains
    assert profile.traits == traits


def test_explicit_fields_override_defaults_and_are_not_mutation_inferred():
    """Explicit axes win while text cannot grant a mutating execution mode."""
    profile = normalize_task_profile(
        legacy_mode="implementation",
        work_type="verify",
        execution_mode="read_only",
        risk="low",
        rigor="direct",
        task_text="overwrite the table and change schema",
    )
    assert (profile.work_type, profile.execution_mode, profile.risk, profile.rigor) == (
        "verify", "read_only", "low", "direct",
    )
    assert len(profile.normalization_notes) >= 5


def test_material_text_selects_full_but_generic_modes_remain_compact():
    """Only concrete material signals, not broad labels, infer full rigor."""
    assert normalize_task_profile(legacy_mode="migration").rigor == "compact"
    assert normalize_task_profile(legacy_mode="greenfield").rigor == "compact"
    assert normalize_task_profile(legacy_mode="etl").rigor == "compact"
    assert normalize_task_profile(legacy_mode="analysis", task_text="destructive overwrite").rigor == "full"


@pytest.mark.parametrize("text", ["publish to Asana", "create a team visible ticket", "draft a work item"])
def test_team_work_item_language_is_classified_as_team_visible(text):
    assert "team-visible" in normalize_task_profile(task_text=text).traits


@pytest.mark.parametrize("text", [
    "Do not create an Asana ticket.",
    "Review the work-item parser.",
    "Fix ticket tokenization.",
    "Document why this work item remains local.",
])
def test_referential_or_negated_work_item_language_is_not_team_visible(text):
    assert "team-visible" not in normalize_task_profile(task_text=text).traits


def test_explicit_traits_are_authoritative_for_team_visibility():
    assert "team-visible" not in normalize_task_profile(
        task_text="create an Asana ticket", traits=["implementation"],
    ).traits


def test_serialization_is_stable_and_evidence_is_immutable():
    """Sets serialize in stable order and evidence retains declaration order."""
    evidence = EvidenceRequest("b", "test", "Run focused tests", "gate")
    profile = normalize_task_profile(
        traits=["zeta", "alpha", "alpha"],
        domains=["CODE", "code"],
        caller_required_evidence=[
            evidence,
            {"id": "a", "kind": "review", "description": "Review", "required_before": "final"},
        ],
    )
    encoded = json.dumps(profile.to_dict(), sort_keys=True)
    assert json.dumps(profile.to_dict(), sort_keys=True) == encoded
    assert profile.to_dict()["traits"] == ["alpha", "zeta"]
    assert [item.id for item in profile.caller_required_evidence] == ["b", "a"]
    with pytest.raises(Exception):
        profile.risk = "high"


@pytest.mark.parametrize(
    "field,value",
    [("work_type", "migration"), ("execution_mode", "mixed"), ("risk", "critical"), ("rigor", "extreme")],
)
def test_invalid_explicit_enum_values_fail(field, value):
    """Unknown explicit enum values fail instead of degrading to defaults."""
    with pytest.raises(ValueError):
        normalize_task_profile(**{field: value})


def test_critical_priority_is_compatibility_metadata_not_a_risk_enum():
    """Legacy critical priority maps onto approved risk and trait values."""
    profile = normalize_task_profile(priority="critical")
    assert profile.risk == "high"
    assert "critical-impact" in profile.traits
    explicit = normalize_task_profile(priority="critical", risk="low")
    assert explicit.risk == "low"
    assert "critical-impact" in explicit.traits


def test_deserialization_validates_instead_of_trusting_casts():
    serialized = normalize_task_profile(legacy_mode="analysis").to_dict()
    assert TaskProfile.from_dict(serialized).to_dict() == serialized
    serialized["execution_mode"] = "mixed"
    with pytest.raises(ValueError):
        TaskProfile.from_dict(serialized)
    with pytest.raises(ValueError):
        TaskProfile.from_dict({"schema_version": "1.0"})


@pytest.mark.parametrize("field", ["legacy_mode", "normalization_notes"])
def test_deserialization_requires_every_emitted_field(field):
    serialized = normalize_task_profile().to_dict()
    serialized.pop(field)
    with pytest.raises(ValueError):
        TaskProfile.from_dict(serialized)


@pytest.mark.parametrize("field", ["domains", "traits", "normalization_notes", "caller_required_evidence"])
def test_deserialization_requires_json_arrays(field):
    serialized = normalize_task_profile().to_dict()
    serialized[field] = "not-an-array"
    with pytest.raises(ValueError, match="JSON array"):
        TaskProfile.from_dict(serialized)


@pytest.mark.parametrize("field,value", [
    ("domains", ["CODE"]), ("domains", ["code_base"]), ("domains", ["code", "code"]),
    ("traits", ["High-Risk"]), ("traits", ["high_risk"]), ("traits", ["risk", "risk"]),
])
def test_deserialization_rejects_noncanonical_and_duplicate_labels(field, value):
    serialized = normalize_task_profile().to_dict()
    serialized[field] = value
    with pytest.raises(ValueError):
        TaskProfile.from_dict(serialized)


def test_direct_construction_rejects_mutable_or_noncanonical_containers():
    values = normalize_task_profile().to_dict()
    values["domains"] = ("general",)
    values["traits"] = frozenset()
    values["caller_required_evidence"] = ()
    values["normalization_notes"] = tuple(values["normalization_notes"])
    TaskProfile(**values)
    for field, bad in (("domains", ["general"]), ("traits", set()), ("traits", ()),
                       ("normalization_notes", []), ("caller_required_evidence", [])):
        invalid = dict(values)
        invalid[field] = bad
        with pytest.raises(ValueError):
            TaskProfile(**invalid)
