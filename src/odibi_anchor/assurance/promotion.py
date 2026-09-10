"""Pure per-control promotion, governed-exception, and rollback assessment."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from odibi_anchor.assurance.models import utc_datetime
from odibi_anchor.assurance.qualification import OutcomeComparison, canonical_json_bytes

AssuranceMode = Literal["shadow", "warn", "block"]
PromotionDisposition = Literal["eligible", "hold", "rollback", "invalid"]

_MODES = ("shadow", "warn", "block")
_ROLLBACK_TRIGGERS = {
    "policy_digest_mismatch",
    "evaluator_leakage",
    "critical_false_positive",
    "critical_escaped_defect",
    "unavailable_treated_as_pass",
    "expired_approval",
}
_MINIMUM_FIELDS = {
    "minimum_agent_families",
    "minimum_dogfood_tasks",
    "minimum_elapsed_days",
    "minimum_hosts",
    "minimum_independent_agents",
    "minimum_real_tasks",
    "minimum_tiers",
}


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = expected.difference(value)
    extra = set(value).difference(expected)
    if missing:
        raise ValueError(f"Missing {label} fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"Unexpected {label} fields: {sorted(extra)}")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"policy contains a non-JSON value: {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _policy_digest(policy_version: str, controls: Mapping[str, Any]) -> str:
    payload = {"controls": _plain_json(controls), "policy_version": policy_version}
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class GovernedException:
    exception_id: str
    control_version: str
    mode: AssuranceMode
    repository_scope: str
    task_scope: str
    subject_scope: str
    rationale: str
    owner: str
    compensating_control: str
    requester: str
    approver: str
    issue_link: str
    created_at: str
    expires_at: str
    use_count: int
    signature: str

    def validate(self, *, now: str, control_version: str, mode: AssuranceMode) -> tuple[str, ...]:
        failures: list[str] = []
        for field in self.__dataclass_fields__:
            if field == "use_count":
                continue
            try:
                _nonempty(getattr(self, field), field)
            except ValueError:
                failures.append(f"exception.{field}:missing")
        if self.control_version != control_version or self.mode != mode:
            failures.append("exception:control_or_mode_mismatch")
        if self.requester == self.approver:
            failures.append("exception:approval_not_independent")
        if not isinstance(self.use_count, int) or self.use_count < 0:
            failures.append("exception:invalid_use_count")
        try:
            created = utc_datetime(self.created_at, "created_at")
            expires = utc_datetime(self.expires_at, "expires_at")
            current = utc_datetime(now, "now")
            if expires <= current:
                failures.append("exception:expired")
            if expires <= created or expires - created > timedelta(days=30):
                failures.append("exception:lifetime_exceeds_30_days")
        except ValueError:
            failures.append("exception:invalid_timestamp")
        return tuple(sorted(set(failures)))


@dataclass(frozen=True)
class PromotionPolicy:
    policy_version: str
    policy_digest: str
    controls: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        _nonempty(self.policy_version, "policy_version")
        if not isinstance(self.controls, Mapping) or not self.controls:
            raise ValueError("controls must be a non-empty object")
        for control_version, control in self.controls.items():
            _nonempty(control_version, "control_version")
            if not isinstance(control, Mapping):
                raise ValueError("each policy control must be an object")
            _exact(control, {"current_mode", "rollback_mode", "warn", "block"}, "policy control")
            if control["current_mode"] not in _MODES or control["rollback_mode"] not in _MODES:
                raise ValueError("policy control modes are invalid")
            for transition in ("warn", "block"):
                minimums = control[transition]
                if not isinstance(minimums, Mapping):
                    raise ValueError("policy transition minimums must be objects")
                _exact(minimums, _MINIMUM_FIELDS, f"policy {transition} minimums")
                if any(type(value) is not int or value < 0 for value in minimums.values()):
                    raise ValueError("policy transition minimums must be non-negative integers")
        if self.policy_digest != _policy_digest(self.policy_version, self.controls):
            raise ValueError("promotion policy digest mismatch")
        object.__setattr__(self, "controls", _freeze_json(_plain_json(self.controls)))

    @classmethod
    def create(cls, policy_version: str, controls: Mapping[str, Mapping[str, Any]]) -> PromotionPolicy:
        """Create a digest-bound immutable policy from reviewed control content."""
        return cls(policy_version, _policy_digest(policy_version, controls), controls)

    @classmethod
    def from_path(cls, path: Path) -> PromotionPolicy:
        raw = path.read_bytes()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("promotion policy is malformed JSON") from exc
        if raw != canonical_json_bytes(value):
            raise ValueError("promotion policy must use canonical JSON")
        _exact(value, {"policy_version", "policy_digest", "controls"}, "PromotionPolicy")
        expected = _policy_digest(value["policy_version"], value["controls"])
        if value["policy_digest"] != expected:
            raise ValueError("promotion policy digest mismatch")
        return cls(**value)


@dataclass(frozen=True)
class PromotionPacket:
    packet_id: str
    assessed_at: str
    control_version: str
    policy_digest: str
    current_mode: AssuranceMode
    requested_mode: AssuranceMode
    comparison: OutcomeComparison
    complete_matrix: bool
    sealed_holdout: bool
    dogfood_tasks: int
    applicable_real_tasks: int
    elapsed_days: int
    tiers: tuple[str, ...]
    scopes: tuple[str, ...]
    agent_families: tuple[str, ...]
    independent_agents: int
    hosts: tuple[str, ...]
    approvals: tuple[Mapping[str, str], ...]
    remediation_proven: bool
    rollback_demonstrated: bool
    critical_escaped_defects: int
    critical_false_positives: int
    unavailable_treated_as_pass: int
    evaluator_leakage: bool
    open_outcome_regression: bool
    ceremony_within_budget: bool
    synthetic: bool
    approval_expired: bool = False
    guardrail_breach: bool = False
    exceptions: tuple[GovernedException, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.packet_id, "packet_id")
        utc_datetime(self.assessed_at, "assessed_at")
        _nonempty(self.control_version, "control_version")
        _nonempty(self.policy_digest, "policy_digest")
        if self.current_mode not in _MODES or self.requested_mode not in _MODES:
            raise ValueError("unsupported assurance mode")
        if not isinstance(self.comparison, OutcomeComparison):
            raise TypeError("comparison must be an OutcomeComparison")
        for field in (
            "dogfood_tasks",
            "applicable_real_tasks",
            "elapsed_days",
            "independent_agents",
            "critical_escaped_defects",
            "critical_false_positives",
            "unavailable_treated_as_pass",
        ):
            if not isinstance(getattr(self, field), int) or getattr(self, field) < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        for field in ("tiers", "scopes", "agent_families", "hosts"):
            value = getattr(self, field)
            if not isinstance(value, tuple) or len(set(value)) != len(value):
                raise ValueError(f"{field} must be a unique tuple")
        approvals = tuple(MappingProxyType(dict(item)) for item in self.approvals)
        object.__setattr__(self, "approvals", approvals)
        if not isinstance(self.exceptions, tuple) or any(
            not isinstance(item, GovernedException) for item in self.exceptions
        ):
            raise TypeError("exceptions must be a tuple of GovernedException values")


@dataclass(frozen=True)
class PromotionDecision:
    disposition: PromotionDisposition
    control_version: str
    current_mode: AssuranceMode
    resulting_mode: AssuranceMode
    failed_criteria: tuple[str, ...]
    retained_history: bool = True
    requires_new_packet: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_version": self.control_version,
            "current_mode": self.current_mode,
            "disposition": self.disposition,
            "failed_criteria": list(self.failed_criteria),
            "requires_new_packet": self.requires_new_packet,
            "resulting_mode": self.resulting_mode,
            "retained_history": self.retained_history,
        }


def _approval_failures(packet: PromotionPacket, now: str) -> list[str]:
    failures: list[str] = []
    roles: set[str] = set()
    identities: set[str] = set()
    for approval in packet.approvals:
        if set(approval) != {"approval_id", "reviewer_id", "role", "implemented_control", "expires_at"}:
            failures.append("approvals:malformed")
            continue
        if approval["implemented_control"] != "false":
            failures.append("approvals:implementer_not_independent")
        try:
            if utc_datetime(approval["expires_at"], "approval.expires_at") <= utc_datetime(now, "now"):
                failures.append("approvals:expired")
        except ValueError:
            failures.append("approvals:invalid_timestamp")
        roles.add(approval["role"])
        identities.add(approval["reviewer_id"])
    if not {"assurance_reviewer", "maintainer"}.issubset(roles) or len(identities) < 2:
        failures.append("approvals:two_independent_roles_required")
    return failures


def assess_promotion(
    packet: PromotionPacket,
    policy: PromotionPolicy,
) -> PromotionDecision:
    """Assess one exact control version; no aggregate can override one failed criterion."""
    control = policy.controls.get(packet.control_version)
    if control is None:
        return PromotionDecision(
            "invalid",
            packet.control_version,
            packet.current_mode,
            packet.current_mode,
            ("control_version:not_in_policy",),
        )
    if control.get("current_mode") != packet.current_mode:
        return PromotionDecision(
            "invalid",
            packet.control_version,
            packet.current_mode,
            packet.current_mode,
            ("current_mode:policy_mismatch",),
        )
    triggers: list[str] = []
    if packet.policy_digest != policy.policy_digest:
        triggers.append("policy_digest_mismatch")
    if packet.evaluator_leakage:
        triggers.append("evaluator_leakage")
    if packet.critical_false_positives:
        triggers.append("critical_false_positive")
    if packet.critical_escaped_defects:
        triggers.append("critical_escaped_defect")
    if packet.unavailable_treated_as_pass:
        triggers.append("unavailable_treated_as_pass")
    if packet.approval_expired:
        triggers.append("expired_approval")
    if triggers and packet.current_mode == "block":
        return PromotionDecision(
            "rollback",
            packet.control_version,
            "block",
            "warn",
            tuple(sorted(triggers)),
        )
    if packet.guardrail_breach and packet.current_mode == "warn":
        return PromotionDecision(
            "rollback",
            packet.control_version,
            "warn",
            "shadow",
            ("guardrail_breach",),
        )
    current_index = _MODES.index(packet.current_mode)
    requested_index = _MODES.index(packet.requested_mode)
    if requested_index != current_index + 1:
        return PromotionDecision(
            "invalid",
            packet.control_version,
            packet.current_mode,
            packet.current_mode,
            ("transition:must_be_one_forward_step",),
        )
    minimums = control.get(packet.requested_mode)
    if not isinstance(minimums, Mapping):
        return PromotionDecision(
            "invalid",
            packet.control_version,
            packet.current_mode,
            packet.current_mode,
            ("policy:transition_not_configured",),
        )
    failures = triggers
    checks = {
        "matrix:incomplete": packet.complete_matrix,
        "holdout:missing": packet.sealed_holdout,
        "comparison:control_version_mismatch": (
            packet.comparison.candidate_cohort.get("control_version") == packet.control_version
            and packet.comparison.baseline_cohort.get("control_version") == packet.control_version
        ),
        "comparison:not_comparable": packet.comparison.comparable,
        "comparison:regression": not packet.comparison.regressions and not packet.open_outcome_regression,
        "critical:comparison_failure": not packet.comparison.critical_failures,
        "ceremony:over_budget": packet.ceremony_within_budget,
        "dogfood:insufficient": packet.dogfood_tasks >= int(minimums["minimum_dogfood_tasks"]),
        "real_tasks:insufficient": packet.applicable_real_tasks >= int(minimums["minimum_real_tasks"]),
        "elapsed_window:insufficient": packet.elapsed_days >= int(minimums["minimum_elapsed_days"]),
        "tier_coverage:insufficient": len(set(packet.tiers)) >= int(minimums["minimum_tiers"]),
        "scope_coverage:incomplete": set(packet.scopes) == {"localized", "systemic"},
        "agent_coverage:insufficient": (
            len(packet.agent_families) >= int(minimums["minimum_agent_families"])
            and (
                len(packet.agent_families) > 1
                or packet.independent_agents >= int(minimums["minimum_independent_agents"])
            )
        ),
        "host_coverage:insufficient": len(packet.hosts) >= int(minimums["minimum_hosts"]),
    }
    if packet.requested_mode == "block":
        checks.update(
            {
                "remediation:not_proven": packet.remediation_proven,
                "rollback:not_demonstrated": packet.rollback_demonstrated,
            }
        )
    failures.extend(key for key, passed in checks.items() if not passed)
    failures.extend(_approval_failures(packet, packet.assessed_at))
    if packet.synthetic:
        failures.append("evidence:synthetic_infrastructure_only")
    for exception in packet.exceptions:
        failures.extend(
            exception.validate(
                now=packet.assessed_at,
                control_version=packet.control_version,
                mode=packet.current_mode,
            )
        )
    return PromotionDecision(
        "hold" if failures else "eligible",
        packet.control_version,
        packet.current_mode,
        packet.current_mode if failures else packet.requested_mode,
        tuple(sorted(set(failures))),
    )


def rollback_triggers() -> frozenset[str]:
    """Return the closed automatic rollback-trigger vocabulary."""
    return frozenset(_ROLLBACK_TRIGGERS)
