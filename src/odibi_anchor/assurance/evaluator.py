"""Pure plan construction, evidence normalization, and shadow evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from odibi_anchor.assurance.catalog import CATALOG_VERSION, CORE_CONTROLS, T1_CONTROLS
from odibi_anchor.assurance.models import (
    ASSURANCE_SCHEMA_VERSION,
    QUALITY_ATTRIBUTE_ORDER,
    AssuranceAssessment,
    AssuranceException,
    AssurancePlan,
    CommandResultEvidence,
    ControlResult,
    EvidenceState,
    QualityAttribute,
    utc_datetime,
)
from odibi_anchor.assurance.overlays import (
    OVERLAY_CONTROLS,
    OVERLAYS,
    OverlayControlResult,
    OverlayException,
    OverlayInput,
    OverlaySelection,
    select_overlays,
)

_T3_TRAITS = frozenset({"critical-impact", "destructive", "irreversible"})
_T2_TRAITS = frozenset({
    "schema-change", "public-contract-change", "cross-system", "material-migration", "rollback-design",
})
_RESULT_ACTIONS = frozenset({
    "task", "test", "preflight", "review", "diff", "quality", "validate", "reconcile",
    "profile_table", "microscope", "case_file", "dogfood",
})
_OUTCOME_KINDS = frozenset({
    "test", "quality", "validate", "reconcile", "outcome", "output", "result", "inspection", "dogfood",
})
_STATE_PRECEDENCE: dict[str, int] = {
    "failed": 5,
    "stale": 4,
    "unavailable": 3,
    "missing": 2,
    "satisfied": 1,
}
_DEFAULT_RESULT_MAX_AGE = timedelta(hours=24)
_ZERO_DIGEST = "sha256:" + "0" * 64
_DISTRIBUTION_SUBCHECKS = frozenset({
    "build", "metadata", "isolated-install", "import", "cli", "installed-distribution",
})


def build_assurance_plan(profile: Any) -> AssurancePlan:
    """Build the v1 plan using only normalized TaskProfile fields."""
    required = ("risk", "execution_mode", "traits", "domains")
    if any(not hasattr(profile, field) for field in required):
        raise TypeError("profile must provide the immutable TaskProfile contract")
    traits = frozenset(profile.traits)
    mode = profile.execution_mode
    risk = profile.risk

    if traits & _T3_TRAITS or (risk == "high" and mode == "data_change"):
        tier = "T3"
        tier_rule = "tier.t3.critical-consequence"
    elif risk == "high" or mode in {"source_change", "data_change"} or traits & _T2_TRAITS:
        tier = "T2"
        tier_rule = "tier.t2.material-change"
    elif risk == "medium" or mode == "artifact_only":
        tier = "T1"
        tier_rule = "tier.t1.focused-assurance"
    else:
        tier = "T0"
        tier_rule = "tier.t0.low-risk-read-only"

    selected: set[QualityAttribute] = {"functional-suitability"}
    mutating = mode in {"source_change", "data_change"}
    if tier in {"T2", "T3"} or mutating:
        selected.update({"reliability", "safety"})
    if mode == "source_change":
        selected.update({"maintainability", "adaptability", "performance-efficiency"})
    if traits & {"schema-change", "public-contract-change", "cross-system"}:
        selected.add("compatibility")
    if "security" in profile.domains or "security" in traits or tier == "T3":
        selected.add("security")
    if mode == "artifact_only" or set(profile.domains) & {"interaction", "ui"}:
        selected.add("interaction-quality")
    attributes = cast(
        tuple[QualityAttribute, ...],
        tuple(attribute for attribute in QUALITY_ATTRIBUTE_ORDER if attribute in selected),
    )

    controls = ["AK-001"]
    if "compatibility" in selected:
        controls.append("AK-002")
    if "maintainability" in selected:
        controls.append("AK-003")
    if "safety" in selected:
        controls.append("AK-004")
    if "reliability" in selected:
        controls.append("AK-005")
    if tier == "T3" and mutating:
        controls.append("AK-006")
    rationale = {tier_rule}
    rationale.update(f"attribute.{attribute}" for attribute in attributes)
    return AssurancePlan(
        schema_version=ASSURANCE_SCHEMA_VERSION,
        catalog_version=CATALOG_VERSION,
        tier=cast(Any, tier),
        quality_attributes=attributes,
        controls=tuple(sorted(controls)),
        rationale=tuple(sorted(rationale)),
    )


def _synthetic_observed_at(index: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=index)).isoformat()


def _valid_observed_at(value: Any) -> bool:
    try:
        utc_datetime(value, "observed_at")
    except (TypeError, ValueError):
        return False
    return True


def _entry_observation(entry: Any, *, caller: bool) -> dict[str, Any] | None:
    if any(not isinstance(getattr(entry, field, None), str) or not getattr(entry, field).strip()
           for field in ("id", "kind", "source")):
        return None
    raw_status = getattr(entry, "status", None)
    status = raw_status if isinstance(raw_status, str) else "unknown"
    state = {"pass": "satisfied", "fail": "failed", "unknown": "unavailable"}.get(status)
    if state is None:
        state = "unavailable"
    observed_at = getattr(entry, "observed_at", None)
    reasons: list[str] = []
    if not _valid_observed_at(observed_at):
        if state != "failed":
            state = "unavailable"
        observed_at = "1970-01-01T00:00:00+00:00"
        reasons.append("evidence.invalid-observed-at")
    provenance = getattr(entry, "provenance", {})
    if not isinstance(provenance, Mapping):
        provenance = {}
        if state != "failed":
            state = "unavailable"
        reasons.append("evidence.invalid-provenance")
    return {
        "id": entry.id,
        "kind": entry.kind,
        "state": state,
        "source": entry.source,
        "observed_at": observed_at,
        "provenance": _json_detach(provenance),
        "lane": "caller" if caller else "control",
        "reasons": reasons,
    }


def _timing_observation(timing: Mapping[str, Any], index: int, epoch: int | None) -> dict[str, Any] | None:
    action = timing.get("action")
    if not isinstance(action, str) or not action:
        return None
    failed = timing.get("error") is not None or timing.get("passed") is False
    current_task = action == "task" and epoch is not None and index == epoch - 1
    stale = epoch is not None and index < epoch and not current_task
    result_value = timing.get("result")
    exit_code = timing.get("exit_code")
    result_bearing = (
        current_task
        or (isinstance(result_value, str) and bool(result_value.strip()))
        or (action == "test" and isinstance(exit_code, int) and not isinstance(exit_code, bool))
    )
    if failed:
        state = "failed"
    elif stale:
        state = "stale"
    elif timing.get("unavailable") is True:
        state = "unavailable"
    elif timing.get("passed") is True and action in _RESULT_ACTIONS and result_bearing:
        state = "satisfied"
    else:
        state = "unavailable"
    observed_at = timing.get("observed_at")
    provenance: dict[str, Any] = {"timing_index": index}
    if not _valid_observed_at(observed_at):
        observed_at = _synthetic_observed_at(index)
        provenance["observed_at_source"] = "session-sequence"
    for source, target in (
        ("test_target", "target"),
        ("test_mark", "mark"),
        ("exit_code", "exit_code"),
        ("result", "result"),
    ):
        if source in timing:
            provenance[target] = _json_detach(timing[source])
    return {
        "id": f"dispatch:{action}:{index}",
        "kind": action,
        "state": state,
        "source": f"anchor:{action}",
        "observed_at": observed_at,
        "provenance": provenance,
        "lane": "control",
        "reasons": ["evidence.pre-task-or-superseded"] if stale else [],
    }


def _json_detach(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_detach(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_detach(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_detach(item) for item in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_assurance_evidence(
    context: Any,
    timings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Normalize current ledger and dispatcher facts without mutating either."""
    requests = tuple(getattr(context, "caller_required_evidence", ()))
    caller_ids = tuple(sorted({request.id for request in requests}))
    caller: dict[str, dict[str, Any]] = {}
    control: dict[str, dict[str, Any]] = {}
    for entry in (
        *tuple(getattr(context, "current_evidence_entries", ())),
        *tuple(getattr(context, "guidance_attestations", ())),
    ):
        is_caller = getattr(entry, "id", None) in caller_ids
        observation = _entry_observation(entry, caller=is_caller)
        if observation is not None:
            (caller if is_caller else control)[observation["id"]] = observation
    epoch = getattr(context, "task_verification_epoch", None)
    if isinstance(epoch, bool) or (epoch is not None and not isinstance(epoch, int)):
        epoch = None
    for index, timing in enumerate(timings):
        if not isinstance(timing, Mapping):
            continue
        observation = _timing_observation(timing, index, epoch)
        if observation is not None:
            control[observation["id"]] = observation
    return {
        "current_action": getattr(context, "current_action", None),
        "caller_required_ids": list(caller_ids),
        "caller": {key: caller[key] for key in sorted(caller)},
        "control": {key: control[key] for key in sorted(control)},
    }


def normalize_command_result_evidence(
    value: Mapping[str, Any],
    *,
    expected_check_id: str | None = None,
    expected_subject_digest: str | None = None,
    expected_scope_digest: str | None = None,
) -> CommandResultEvidence:
    """Normalize one exact command result, degrading malformed input to unavailable."""
    try:
        evidence = CommandResultEvidence.from_dict(value)
        mismatches = []
        if expected_check_id is not None and evidence.check_id != expected_check_id:
            mismatches.append("check-id")
        if expected_subject_digest is not None and evidence.subject_digest != expected_subject_digest:
            mismatches.append("subject-digest")
        if expected_scope_digest is not None and evidence.scope_digest != expected_scope_digest:
            mismatches.append("scope-digest")
        if not mismatches:
            return evidence
        raw = evidence.to_dict()
        raw["state"] = "unavailable"
        raw["check_id"] = expected_check_id or evidence.check_id
        raw["subject_digest"] = expected_subject_digest or evidence.subject_digest
        raw["scope_digest"] = expected_scope_digest or evidence.scope_digest
        raw["provenance"] = {
            **raw["provenance"], "normalization_errors": mismatches,
        }
        return CommandResultEvidence.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        timestamp = "1970-01-01T00:00:00+00:00"
        evidence_id = value.get("evidence_id") if isinstance(value, Mapping) else None
        return CommandResultEvidence(
            evidence_id=evidence_id if isinstance(evidence_id, str) and evidence_id else "unavailable",
            check_id=expected_check_id or "unavailable",
            state="unavailable",
            observed_at=timestamp,
            completed_at=timestamp,
            subject_digest=expected_subject_digest or _ZERO_DIGEST,
            scope_digest=expected_scope_digest or _ZERO_DIGEST,
            producer="adapter",
            producer_version="1",
            result={},
            provenance={"normalization_errors": ["malformed-command-result"]},
        )


def _t1_applicable(control_id: str, paths: tuple[str, ...], implementation_delivery: bool) -> bool:
    python_paths = tuple(path for path in paths if path.endswith(".py"))
    if control_id == "Anchor-T1-TESTS":
        return bool(python_paths or any(path.startswith("tests/") for path in paths))
    if control_id == "Anchor-T1-STATIC-RATCHET":
        return any(path.split("/", 1)[0] in {"src", "tests", "tools", "scripts"}
                   for path in python_paths)
    if control_id == "Anchor-T1-OUTPUT-CONTRACT":
        return any(
            path == "OUTPUT_SCHEMA.md"
            or path == "scripts/verify_outputs.py"
            or path.startswith("tests/test_outputs")
            or path.startswith("src/odibi_anchor/_utils/contract")
            for path in paths
        )
    if control_id == "Anchor-T1-DISTRIBUTION":
        return any(
            path in {"pyproject.toml", "setup.py", "MANIFEST.in"}
            or path.startswith((".github/", ".assistant/", "src/odibi_anchor/cli.py",
                                "src/odibi_anchor/__main__.py"))
            or path in {"scripts/verify_distribution.py", "tests/test_installed_distribution.py"}
            for path in paths
        )
    return implementation_delivery


def _result_semantics(check_id: str, result: Mapping[str, Any]) -> bool | None:
    if check_id == "tests":
        required = ("exit_code", "failed", "errors", "expected_scope_ran")
        if any(key not in result for key in required):
            return None
        return (
            result["exit_code"] == 0 and result["failed"] == 0
            and result["errors"] == 0 and result["expected_scope_ran"] is True
        )
    if check_id in {"static-ruff", "static-pyright"}:
        findings = result.get("findings")
        if not isinstance(findings, Mapping) or any(
            key not in findings or not isinstance(findings[key], (tuple, list))
            for key in ("introduced", "unknown")
        ):
            return None
        return not findings["introduced"] and not findings["unknown"]
    if check_id == "output-contracts":
        probes = result.get("probes")
        if result.get("exit_code") is None or not isinstance(probes, Mapping) or not probes:
            return None
        return result["exit_code"] == 0 and all(value == "pass" for value in probes.values())
    if check_id == "distribution":
        subchecks = result.get("subchecks")
        if not isinstance(subchecks, Mapping) or not _DISTRIBUTION_SUBCHECKS.issubset(subchecks):
            return None
        return all(subchecks[name] == "pass" for name in _DISTRIBUTION_SUBCHECKS)
    if check_id == "scope-integrity":
        required = ("intended_paths_known", "reconciled", "denied_paths", "unattested_paths")
        if any(key not in result for key in required):
            return None
        if not isinstance(result["denied_paths"], (tuple, list)) or not isinstance(
            result["unattested_paths"], (tuple, list)
        ):
            return None
        return (
            result["intended_paths_known"] is True and result["reconciled"] is True
            and not result["denied_paths"] and not result["unattested_paths"]
        )
    return None


def _required_check_ids(control_id: str) -> tuple[str, ...]:
    return {
        "Anchor-T1-TESTS": ("tests",),
        "Anchor-T1-STATIC-RATCHET": ("static-ruff", "static-pyright"),
        "Anchor-T1-OUTPUT-CONTRACT": ("output-contracts",),
        "Anchor-T1-DISTRIBUTION": ("distribution",),
        "Anchor-T1-SCOPE-INTEGRITY": ("scope-integrity",),
    }[control_id]


def evaluate_t1_controls(
    changed_paths: Sequence[str],
    evidence: Sequence[CommandResultEvidence | Mapping[str, Any]],
    *,
    subject_digest: str,
    scope_digest: str,
    implementation_delivery: bool = True,
    now: datetime | None = None,
    max_age: timedelta = _DEFAULT_RESULT_MAX_AGE,
    controls: Sequence[Any] = T1_CONTROLS,
) -> tuple[ControlResult, ...]:
    """Evaluate exactly the result-backed T1 controls without granting authority."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("now must be UTC")
    paths = tuple(sorted(set(changed_paths)))
    normalized = tuple(
        item if isinstance(item, CommandResultEvidence) else normalize_command_result_evidence(item)
        for item in evidence
    )
    results: list[ControlResult] = []
    for definition in controls:
        control_id = definition.control_id
        if not _t1_applicable(control_id, paths, implementation_delivery):
            results.append(ControlResult(
                control_id, "not_applicable", "missing", definition.disposition,
                (), None, ("control.not-applicable",),
            ))
            continue
        selected: list[CommandResultEvidence] = []
        states: list[str] = []
        reasons = ["control.applicable"]
        for check_id in _required_check_ids(control_id):
            matching = [item for item in normalized if item.check_id == check_id]
            if not matching:
                states.append("missing")
                reasons.append(f"evidence.missing:{check_id}")
                continue
            selected.extend(matching)
            for item in matching:
                if item.state == "failed":
                    states.append("failed")
                elif item.subject_digest != subject_digest or item.scope_digest != scope_digest:
                    states.append("unavailable")
                    reasons.append(f"evidence.binding-mismatch:{item.evidence_id}")
                elif not item.is_fresh(now, max_age):
                    states.append("stale")
                elif item.state != "satisfied":
                    states.append(item.state)
                else:
                    semantic = _result_semantics(check_id, item.result)
                    states.append("satisfied" if semantic is True else "failed" if semantic is False else "unavailable")
        state = cast(EvidenceState, max(states, key=lambda item: _STATE_PRECEDENCE[item]))
        reasons.append(f"evidence.{state}")
        results.append(ControlResult(
            control_id=control_id,
            applicability="applicable",
            evidence_state=state,
            disposition=definition.disposition,
            evidence_ids=tuple(sorted({item.evidence_id for item in selected})),
            exception_id=None,
            reasons=tuple(reasons),
        ))
    return tuple(results)


def _canonical_observations(evidence: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    observations: list[dict[str, Any]] = []
    for lane in ("caller", "control"):
        values = evidence.get(lane, {}) if isinstance(evidence, Mapping) else {}
        if not isinstance(values, Mapping):
            continue
        for evidence_id, raw in sorted(values.items(), key=lambda item: str(item[0])):
            if not isinstance(evidence_id, str) or not isinstance(raw, Mapping):
                continue
            required = {"id", "kind", "state", "source", "observed_at", "provenance"}
            if not required.issubset(raw) or raw.get("id") != evidence_id:
                continue
            if raw.get("state") not in _STATE_PRECEDENCE:
                continue
            if any(not isinstance(raw.get(field), str) or not raw.get(field) for field in ("kind", "source")):
                continue
            if not _valid_observed_at(raw.get("observed_at")) or not isinstance(raw.get("provenance"), Mapping):
                continue
            observations.append({
                "id": evidence_id,
                "kind": raw["kind"],
                "state": raw["state"],
                "source": raw["source"],
                "observed_at": raw["observed_at"],
                "provenance": _json_detach(raw["provenance"]),
                "lane": lane,
            })
    return tuple(observations)


def _state(observations: Sequence[Mapping[str, Any]], *, missing: bool = False) -> EvidenceState:
    states = [str(item["state"]) for item in observations]
    if missing:
        states.append("missing")
    if not states:
        return "missing"
    return cast(EvidenceState, max(states, key=lambda item: _STATE_PRECEDENCE[item]))


def _current_or_stale(observations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use superseded observations only when no current observation exists."""
    current = [item for item in observations if item["state"] != "stale"]
    return current or list(observations)


def _latest(observations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return max(
        observations,
        key=lambda item: (utc_datetime(item["observed_at"]), item["id"]),
    )


def _control_evidence(
    control_id: str,
    observations: tuple[dict[str, Any], ...],
    evidence: Mapping[str, Any],
) -> tuple[EvidenceState, tuple[str, ...], tuple[str, ...]]:
    missing = False
    reasons: list[str] = []
    selected: list[dict[str, Any]] = []
    if control_id == "AK-001":
        selected = _current_or_stale([
            item for item in observations
            if item["kind"] in _RESULT_ACTIONS
            and not (evidence.get("current_action") == "gate" and item["kind"] == "task")
        ])
    elif control_id == "AK-002":
        required = evidence.get("caller_required_ids", [])
        required = required if isinstance(required, list) else []
        caller = {item["id"]: item for item in observations if item["lane"] == "caller"}
        if not required:
            missing = True
            reasons.append("evidence.no-caller-compatibility-request")
        for evidence_id in required:
            if evidence_id in caller:
                selected.append(caller[evidence_id])
            else:
                missing = True
        all_ids = {item["id"] for item in observations}
        for item in selected:
            references = item["provenance"].get("referenced_ids", [])
            if not isinstance(references, list) or any(reference not in all_ids for reference in references):
                missing = True
                reasons.append("evidence.unresolved-reference")
    elif control_id == "AK-003":
        tests = [item for item in observations if item["kind"] == "test"]
        candidates = _current_or_stale(tests)
        selected = [_latest(candidates)] if candidates else []
        if selected and selected[0]["state"] == "satisfied":
            provenance = selected[0]["provenance"]
            if provenance.get("mark") or not provenance.get("target") or provenance.get("exit_code") != 0:
                selected[0] = {**selected[0], "state": "unavailable"}
                reasons.append("evidence.test-provenance-incomplete-or-filtered")
    elif control_id == "AK-004":
        for kind in ("preflight", "review"):
            matching = [item for item in observations if item["kind"] == kind]
            if matching:
                selected.append(_latest(_current_or_stale(matching)))
            else:
                missing = True
    elif control_id == "AK-005":
        selected = _current_or_stale([
            item for item in observations if item["kind"] in _OUTCOME_KINDS
        ])
    elif control_id == "AK-006":
        selected = _current_or_stale([
            item for item in observations if item["kind"] in {"recovery", "rollback"}
        ])
    state = _state(selected, missing=missing)
    evidence_ids = tuple(sorted(item["id"] for item in selected))
    reasons.insert(0, f"evidence.{state}")
    return state, evidence_ids, tuple(reasons)


def _active_exception(
    control_id: str,
    exceptions: Sequence[AssuranceException | Mapping[str, Any]],
    now: datetime,
) -> tuple[AssuranceException | None, tuple[str, ...]]:
    ignored: list[str] = []
    candidates: list[AssuranceException] = []
    for index, value in enumerate(exceptions):
        try:
            item = value if isinstance(value, AssuranceException) else AssuranceException.from_dict(value)
        except (TypeError, ValueError, KeyError):
            ignored.append(f"exception.ignored-malformed:{index}")
            continue
        if item.control_id != control_id:
            continue
        if item.status != "active":
            ignored.append(f"exception.ignored-{item.status}:{item.exception_id}")
            continue
        if not (utc_datetime(item.created_at) < now < utc_datetime(item.expires_at)):
            ignored.append(f"exception.ignored-expired:{item.exception_id}")
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: item.exception_id)
    return (candidates[0] if candidates else None), tuple(sorted(ignored))


def evaluate_assurance(
    plan: AssurancePlan,
    evidence: Mapping[str, Any],
    exceptions: Sequence[AssuranceException | Mapping[str, Any]] = (),
    *,
    now: datetime | None = None,
) -> AssuranceAssessment:
    """Evaluate every v1 control without producing a gate or authority decision."""
    if not isinstance(plan, AssurancePlan):
        raise TypeError("plan must be an AssurancePlan")
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence must be a mapping")
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("now must be UTC")
    observations = _canonical_observations(evidence)
    results: list[ControlResult] = []
    for definition in CORE_CONTROLS:
        control_id = definition.control_id
        if control_id not in plan.controls:
            results.append(ControlResult(
                control_id=control_id,
                applicability="not_applicable",
                evidence_state="missing",
                disposition=definition.disposition,
                evidence_ids=(),
                exception_id=None,
                reasons=("control.not-applicable",),
            ))
            continue
        state, evidence_ids, reasons = _control_evidence(control_id, observations, evidence)
        exception, ignored = _active_exception(control_id, exceptions, now)
        result_reasons = ["control.applicable", *reasons, *ignored]
        if exception is not None:
            result_reasons.append("exception.active-advisory-only")
        results.append(ControlResult(
            control_id=control_id,
            applicability="applicable",
            evidence_state=state,
            disposition=definition.disposition,
            evidence_ids=evidence_ids,
            exception_id=exception.exception_id if exception else None,
            reasons=tuple(result_reasons),
        ))
    if tuple(result.control_id for result in results) != tuple(
        definition.control_id for definition in CORE_CONTROLS
    ):
        raise RuntimeError("assurance evaluation did not cover the complete v1 catalog")
    return AssuranceAssessment(
        schema_version=ASSURANCE_SCHEMA_VERSION,
        mode="shadow",
        plan=plan,
        results=tuple(results),
    )


def overlay_input_from_profile(
    profile: Any,
    plan: AssurancePlan,
    *,
    declared_effects: Sequence[str] = (),
    changed_path_classes: Sequence[str] = (),
    repository_capabilities: Sequence[str] = (),
) -> OverlayInput:
    """Adapt only recognized immutable task facts into the overlay contract."""
    if not isinstance(plan, AssurancePlan):
        raise TypeError("plan must be an AssurancePlan")
    known_domains = set().union(*(item.predicate.domains_any for item in OVERLAYS))
    known_traits = set().union(*(item.predicate.traits_any for item in OVERLAYS))
    domains = {
        str(value).replace("-", "_") for value in getattr(profile, "domains", ())
    }.intersection(known_domains)
    traits = {
        str(value).replace("-", "_") for value in getattr(profile, "traits", ())
    }.intersection(known_traits)
    work_type = str(getattr(profile, "work_type", "investigate"))
    if work_type not in {"change", "investigate", "operate", "document", "review"}:
        work_type = "investigate"
    return OverlayInput(
        work_type=work_type,
        execution_mode=str(getattr(profile, "execution_mode", "read_only")),
        assurance_tier=plan.tier,
        domains=frozenset(domains),
        traits=frozenset(traits),
        declared_effects=frozenset(declared_effects),
        quality_attributes=frozenset(plan.quality_attributes),
        changed_path_classes=frozenset(changed_path_classes),
        repository_capabilities=frozenset(repository_capabilities),
    )


def _valid_overlay_exception(
    exception: OverlayException,
    control_id: str,
    *,
    scope_digest: str,
    now: datetime,
    evidence_ids: set[str],
) -> bool:
    try:
        expires_at = utc_datetime(exception.expires_at, "expires_at")
    except (TypeError, ValueError):
        return False
    return (
        exception.control_id == control_id
        and exception.scope_digest == scope_digest
        and utc_datetime(exception.created_at, "created_at") < now
        and expires_at > now
        and set(exception.compensating_evidence_ids).issubset(evidence_ids)
    )


def evaluate_overlay_controls(
    overlay_input: OverlayInput,
    evidence: Sequence[CommandResultEvidence | Mapping[str, Any]] = (),
    exceptions: Sequence[OverlayException] = (),
    *,
    subject_digest: str = _ZERO_DIGEST,
    scope_digest: str = _ZERO_DIGEST,
    now: datetime | None = None,
    max_age: timedelta = _DEFAULT_RESULT_MAX_AGE,
) -> tuple[OverlaySelection, tuple[OverlayControlResult, ...]]:
    """Select and assess every overlay control without changing authority or gates."""
    selection = select_overlays(overlay_input)
    selected_overlays = {item.overlay_id for item in selection.selected}
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("now must be UTC")
    normalized = tuple(
        item if isinstance(item, CommandResultEvidence) else normalize_command_result_evidence(item)
        for item in evidence
    )
    evidence_ids = {item.evidence_id for item in normalized}
    results: list[OverlayControlResult] = []
    for control in OVERLAY_CONTROLS:
        if control.overlay_id not in selected_overlays:
            results.append(OverlayControlResult(
                control.control_id, control.version, control.overlay_id, "not_applicable",
                (), None, ("predicate_not_met",),
            ))
            continue
        matching = [item for item in normalized if item.check_id in control.required_evidence]
        by_kind = {
            requirement: [item for item in matching if item.check_id == requirement]
            for requirement in control.required_evidence
        }
        reasons = ["control:applicable"]
        if any(not values for values in by_kind.values()):
            state = "not_assessed"
            reasons.extend(
                f"evidence:missing:{kind}" for kind, values in by_kind.items() if not values
            )
        else:
            states: list[str] = []
            for values in by_kind.values():
                for item in values:
                    if item.state == "failed" or item.result.get("passed") is False:
                        states.append("failed")
                    elif (
                        item.state != "satisfied"
                        or item.subject_digest != subject_digest
                        or item.scope_digest != scope_digest
                        or not item.is_fresh(now, max_age)
                        or item.result.get("passed") is not True
                    ):
                        states.append("unavailable")
                    else:
                        states.append("satisfied")
            state = (
                "failed" if "failed" in states
                else "unavailable" if "unavailable" in states
                else "satisfied"
            )
        exception = next((
            item for item in sorted(exceptions, key=lambda value: (value.control_id, value.owner))
            if _valid_overlay_exception(
                item, control.control_id, scope_digest=scope_digest, now=now,
                evidence_ids=evidence_ids,
            )
        ), None)
        if exception is not None:
            state = "excepted"
            reasons.append("exception:active")
        reasons.append(f"evidence:{state}")
        results.append(OverlayControlResult(
            control.control_id,
            control.version,
            control.overlay_id,
            cast(Any, state),
            tuple(sorted(item.evidence_id for item in matching)),
            exception.owner if exception else None,
            tuple(sorted(set(reasons))),
        ))
    return selection, tuple(results)


def assurance_shadow_metrics(assessment: AssuranceAssessment) -> dict[str, Any]:
    """Project a stable additive summary for gate metrics."""
    applicability = {"applicable": 0, "not_applicable": 0}
    evidence_states = {state: 0 for state in ("satisfied", "failed", "missing", "unavailable", "stale")}
    for result in assessment.results:
        applicability[result.applicability] += 1
        evidence_states[result.evidence_state] += 1
    return {
        "mode": "shadow",
        "status": "evaluated",
        "catalog_version": assessment.plan.catalog_version,
        "tier": assessment.plan.tier,
        "counts": {
            "applicability": applicability,
            "evidence_state": evidence_states,
        },
        "results": [result.to_dict() for result in assessment.results],
    }
