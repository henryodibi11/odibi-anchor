"""Pure Phase-2 task policy contracts and evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, cast

from odibi_anchor.planning._task_profile import EvidenceRequest, TaskProfile

ActionEffect = Literal[
    "orient", "read", "governance_write", "artifact_write", "source_write", "data_write",
    "external_mutation",
]
EffectPermission = Literal[
    "read", "governance_write", "artifact_write", "source_write", "data_write",
    "external_mutation",
]
EvidenceStatus = Literal["pass", "fail", "unknown"]
Disposition = Literal["not_required", "recommended", "required"]
WorkItemDepth = Literal["none", "single", "set"]


def _freeze(value: Any) -> Any:
    """Recursively detach mutable input and expose immutable containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        # asdict() deep-copies leaves before returning.  That is both unnecessary
        # here and fails for deliberately immutable leaves such as MappingProxyType.
        return MappingProxyType({item.name: _freeze(getattr(value, item.name))
                                 for item in fields(value)})
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    raise TypeError(f"unsupported mutable policy value: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    """Convert an immutable contract value to stable JSON-compatible data."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_thaw(item) for item in value), key=str)
    return value


@dataclass(frozen=True)
class BpsKernel:
    """Minimal universal problem-solving state; it does not imply paperwork."""

    problem: str
    intended_outcome: str
    known_facts: tuple[str, ...] = ()
    material_uncertainties: tuple[str, ...] = ()
    next_check: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.problem, str) or not isinstance(self.intended_outcome, str):
            raise TypeError("BPS problem and intended_outcome must be strings")
        if not isinstance(self.next_check, str):
            raise TypeError("BPS next_check must be a string")
        for name in ("known_facts", "material_uncertainties"):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) for item in value):
                raise TypeError(f"BPS {name} must contain only strings")
            object.__setattr__(self, name, tuple(value))

    def to_dict(self) -> dict[str, Any]:
        return {"problem": self.problem, "intended_outcome": self.intended_outcome,
                "known_facts": list(self.known_facts),
                "material_uncertainties": list(self.material_uncertainties), "next_check": self.next_check}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BpsKernel":
        if not isinstance(value, Mapping):
            raise TypeError("BPS payload must be a mapping")
        for key in ("problem", "intended_outcome", "next_check"):
            if not isinstance(value.get(key, ""), str):
                raise TypeError(f"BPS {key} must be a string")
        for key in ("known_facts", "material_uncertainties"):
            items = value.get(key, ())
            if not isinstance(items, (list, tuple)) or any(not isinstance(item, str) for item in items):
                raise TypeError(f"BPS {key} must be a list or tuple of strings")
        return cls(value.get("problem", ""), value.get("intended_outcome", ""),
                   tuple(value.get("known_facts", ())), tuple(value.get("material_uncertainties", ())),
                   value.get("next_check", ""))


@dataclass(frozen=True)
class EvidenceEntry:
    id: str
    kind: str
    status: EvidenceStatus
    source: str
    observed_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "unknown"}:
            raise ValueError(f"Invalid evidence status: {self.status!r}")
        object.__setattr__(self, "provenance", _freeze(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status, "source": self.source,
                "observed_at": self.observed_at, "provenance": _thaw(self.provenance)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceEntry":
        return cls(value["id"], value["kind"], value["status"], value["source"],
                   value["observed_at"], value.get("provenance", {}))


@dataclass(frozen=True)
class ManagedArtifactEntry:
    path: str
    kind: str
    created_or_updated_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", _freeze(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "created_or_updated_at": self.created_or_updated_at,
                "provenance": _thaw(self.provenance)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManagedArtifactEntry":
        return cls(value["path"], value["kind"], value["created_or_updated_at"], value.get("provenance", {}))


@dataclass(frozen=True)
class TaskPolicyContext:
    """Complete detached snapshot consumed by Phase-2 policy rules."""

    profile: TaskProfile
    bps_kernel: BpsKernel
    anchor_root: str | None = None
    project_root: str | None = None
    artifact_root: str | None = None
    target_root: str | None = None
    target_worktree: str | None = None
    explicit_problem_requested: bool = False
    explicit_spec_requested: bool = False
    explicit_pr_draft_requested: bool | None = None
    linked_problem: str | None = None
    linked_spec: str | None = None
    persisted_spec: bool = False
    linked_managed_artifacts: tuple[ManagedArtifactEntry, ...] = ()
    current_phase: int = 1
    phase_count: int = 1
    current_action: str = "task"
    current_effect: ActionEffect = "orient"
    current_effect_permissions: frozenset[EffectPermission] = frozenset()
    referenced_facts: tuple[str, ...] = ()
    caller_required_evidence: tuple[EvidenceRequest, ...] = ()
    current_evidence_entries: tuple[EvidenceEntry, ...] = ()
    guidance_attestations: tuple[EvidenceEntry, ...] = ()
    task_verification_epoch: int | None = None
    checkpoint_final: bool = False
    source_files_changed: tuple[str, ...] = ()
    observed_effects: tuple[ActionEffect, ...] = ()
    intended_pr_paths: tuple[str, ...] = ()
    active_runtime_identifiers: Mapping[str, str] = field(default_factory=dict)
    repository_snapshot: Any | None = None
    repository_pr_config: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        # Module reloads can produce an equivalent frozen TaskProfile class with
        # different identity; validate its immutable contract structurally.
        if (not hasattr(self.profile, "to_dict") or
                self.profile.__class__.__name__ != "TaskProfile" or
                not isinstance(self.bps_kernel, BpsKernel)):
            raise TypeError("profile and bps_kernel must use their immutable contract types")
        for name in ("linked_managed_artifacts", "referenced_facts", "caller_required_evidence",
                     "current_evidence_entries", "guidance_attestations", "source_files_changed",
                     "observed_effects", "intended_pr_paths"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if any(not isinstance(item, ManagedArtifactEntry) for item in self.linked_managed_artifacts):
            raise TypeError("linked_managed_artifacts requires ManagedArtifactEntry values")
        if any(not isinstance(item, EvidenceRequest) for item in self.caller_required_evidence):
            raise TypeError("caller_required_evidence requires EvidenceRequest values")
        if any(not isinstance(item, EvidenceEntry) for item in
               self.current_evidence_entries + self.guidance_attestations):
            raise TypeError("evidence ledgers require EvidenceEntry values")
        if any(not isinstance(item, str) for name in ("referenced_facts", "source_files_changed", "intended_pr_paths")
               for item in getattr(self, name)):
            raise TypeError("fact and path ledgers require strings")
        if self.current_action == "" or not isinstance(self.current_action, str):
            raise ValueError("current_action must be a non-empty string")
        if self.current_effect not in {
            "orient", "read", "governance_write", "artifact_write", "source_write",
            "data_write", "external_mutation",
        }:
            raise ValueError("invalid current_effect")
        if isinstance(self.current_phase, bool) or isinstance(self.phase_count, bool) or not isinstance(self.current_phase, int) or not isinstance(self.phase_count, int):
            raise TypeError("phase values must be integers")
        if self.phase_count < 1 or not 1 <= self.current_phase <= self.phase_count:
            raise ValueError("current_phase must be between 1 and phase_count")
        if self.task_verification_epoch is not None:
            if isinstance(self.task_verification_epoch, bool) or not isinstance(self.task_verification_epoch, int):
                raise TypeError("task_verification_epoch must be an integer or None")
            if self.task_verification_epoch < 0:
                raise ValueError("task_verification_epoch cannot be negative")
        from odibi_anchor._dispatcher._effects import EFFECT_PERMISSIONS
        if frozenset(self.current_effect_permissions) != EFFECT_PERMISSIONS[self.current_effect]:
            raise ValueError("current_effect_permissions must exactly match the current effect")
        object.__setattr__(self, "current_effect_permissions", frozenset(self.current_effect_permissions))
        object.__setattr__(self, "active_runtime_identifiers", _freeze(self.active_runtime_identifiers))
        if self.repository_pr_config is not None:
            object.__setattr__(self, "repository_pr_config", _freeze(self.repository_pr_config))
        if self.repository_snapshot is not None:
            object.__setattr__(self, "repository_snapshot", _freeze(self.repository_snapshot))

    @property
    def current_profile(self) -> TaskProfile:
        """Compatibility alias for the initial Phase-2 prototype."""
        return self.profile


@dataclass(frozen=True)
class PolicyDecision:
    disposition: Disposition
    rule_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    policy_required_evidence: tuple[EvidenceRequest, ...] = ()


@dataclass(frozen=True)
class WorkItemPolicyDecision:
    """Independent recommendation for team-visible work-item artifacts."""

    disposition: Disposition
    depth: WorkItemDepth
    rule_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    policy_required_evidence: tuple[EvidenceRequest, ...] = ()


@dataclass(frozen=True)
class TaskPolicySet:
    problem_record: PolicyDecision
    specification: PolicyDecision
    guidance: PolicyDecision
    pr_readiness: PolicyDecision | None = None
    work_items: WorkItemPolicyDecision = field(default_factory=lambda: WorkItemPolicyDecision(
        "not_required", "none", ("work-item.compatibility-default",),
        ("No work-item policy was supplied by this compatibility caller.",),
    ))


def _decision(disposition: str, rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(cast(Disposition, disposition), (rule,), (reason,))


def evaluate_task_policies(profile: TaskProfile, context: TaskPolicyContext) -> TaskPolicySet:
    """Evaluate only supplied facts; this function deliberately has no session access."""
    persisted_spec_context = bool(context.linked_spec and context.persisted_spec)
    if context.explicit_problem_requested:
        problem = _decision("required", "problem.explicit-or-full", "Explicit request or full rigor requires a Problem Record.")
    elif (persisted_spec_context and profile.execution_mode == "source_change"
          and profile.work_type != "investigate"):
        problem = _decision("not_required", "problem.persisted-spec", "The linked persisted specification supplies durable context.")
    elif profile.rigor == "full":
        problem = _decision("required", "problem.explicit-or-full", "Explicit request or full rigor requires a Problem Record.")
    elif profile.execution_mode == "read_only":
        problem = _decision("not_required", "problem.read-only", "Read-only work has no Problem Record obligation.")
    elif profile.risk == "high" or profile.traits & {"cross-system", "migration", "reconciliation"}:
        problem = _decision("recommended", "problem.complexity", "Risk or decision complexity recommends a Problem Record.")
    else:
        problem = _decision("not_required", "problem.simple", "No material ambiguity requires a Problem Record.")

    consequential = profile.traits & {"schema-change", "public-contract-change", "destructive", "rollback-design", "irreversible"}
    mutating = profile.execution_mode in {"source_change", "data_change"}
    if context.explicit_spec_requested or (mutating and (
        bool(consequential) or context.phase_count > 1 or profile.risk == "high"
        or profile.execution_mode == "data_change"
    )):
        spec = _decision("required", "spec.consequential", "Explicit or consequential implementation requires a specification.")
    elif mutating:
        spec = _decision("recommended", "spec.ordinary-change", "An ordinary implementation change recommends a specification.")
    else:
        spec = _decision("not_required", "spec.non-mutating", "Non-mutating work has no specification obligation.")
    guidance = _decision("recommended", "guidance.compatibility", "Existing Phase-1 guidance gates remain authoritative.")

    work_evidence = tuple(
        item for item in profile.caller_required_evidence if item.kind == "work-item"
    )
    team_visible = bool(
        work_evidence or profile.traits & {"team-visible", "work-item", "work-item-set"}
    )
    complex_work_item = team_visible and (
        context.phase_count > 1 or profile.rigor == "full"
        or bool(profile.traits & {"cross-system", "work-item-set"})
    )
    if work_evidence:
        work_items = WorkItemPolicyDecision(
            "required", "set" if complex_work_item else "single",
            ("work-item.caller-required",),
            ("Caller-required work-item evidence requires a managed work item.",),
            work_evidence,
        )
    elif not team_visible:
        work_items = WorkItemPolicyDecision(
            "not_required", "none", ("work-item.private-or-direct",),
            ("No team-visible work-item intent was supplied.",),
        )
    elif complex_work_item:
        work_items = WorkItemPolicyDecision(
            "required" if profile.risk == "high" else "recommended", "set",
            ("work-item.complex",),
            ("Team-visible multi-phase, full-rigor, or cross-system work uses outcome-level decomposition.",),
        )
    elif profile.risk == "high":
        work_items = WorkItemPolicyDecision(
            "required", "single", ("work-item.high-risk",),
            ("High-risk team-visible work requires a durable work-item draft.",),
        )
    else:
        work_items = WorkItemPolicyDecision(
            "recommended", "single", ("work-item.team-visible",),
            ("Team-visible ordinary work recommends one durable work-item draft.",),
        )
    if context.repository_snapshot is None:
        return TaskPolicySet(
            problem_record=problem, specification=spec, guidance=guidance,
            pr_readiness=None, work_items=work_items,
        )
    snapshot_paths: tuple[str, ...] = ()
    if isinstance(context.repository_snapshot, Mapping):
        snapshot_paths = tuple(context.repository_snapshot.get("changed_paths", ()))
    else:
        snapshot_paths = tuple(getattr(context.repository_snapshot, "changed_paths", ()))
    applies = bool(snapshot_paths) or "source_write" in context.observed_effects
    config_mode = "recommended"
    if isinstance(context.repository_pr_config, Mapping):
        config_mode = context.repository_pr_config.get("pr_readiness", "recommended")
    if not applies:
        pr = _decision("not_required", "pr.no-source-change",
                       "No actual repository source change or observed source-write effect exists.")
    elif context.explicit_pr_draft_requested is True:
        pr = _decision("required", "pr.explicit", "The caller explicitly requested a managed PR draft.")
    elif config_mode == "required":
        pr = _decision("required", "pr.repository-required", "Repository configuration requires PR readiness.")
    elif config_mode == "recommended":
        pr = _decision("recommended", "pr.repository-recommended", "Repository configuration recommends PR readiness.")
    else:
        pr = _decision("not_required", "pr.repository-disabled", "Repository configuration disables automatic PR drafting.")
    return TaskPolicySet(
        problem_record=problem, specification=spec, guidance=guidance,
        pr_readiness=pr, work_items=work_items,
    )


def effective_required_evidence(profile: TaskProfile, policies: TaskPolicySet) -> tuple[EvidenceRequest, ...]:
    """Union caller and policy requests without mutating caller declarations."""
    result = list(profile.caller_required_evidence)
    seen = {item.id for item in result}
    decisions = (
        policies.problem_record, policies.specification, policies.guidance,
        policies.work_items, policies.pr_readiness,
    )
    for decision in decisions:
        if decision is None:
            continue
        for item in decision.policy_required_evidence:
            if item.id not in seen:
                result.append(item)
                seen.add(item.id)
    return tuple(result)


_OMITTED = object()


def build_task_policy_context(
    profile: TaskProfile,
    *,
    session_state: Any,
    current_action: str,
    current_effect: ActionEffect = "orient",
    current_effect_permissions: frozenset[EffectPermission] = frozenset(),
    source_files_changed: tuple[str, ...] = (),
    explicit_problem_requested: bool | object = _OMITTED,
    explicit_spec_requested: bool | object = _OMITTED,
    explicit_pr_draft_requested: bool | None | object = _OMITTED,
    linked_problem: str | None | object = _OMITTED,
    linked_spec: str | None | object = _OMITTED,
    persisted_spec: bool | object = _OMITTED,
    bps_kernel: BpsKernel | object = _OMITTED,
    referenced_facts: tuple[str, ...] | object = _OMITTED,
    checkpoint_final: bool = False,
    current_phase: int | None = None,
    phase_count: int | None = None,
    repository_snapshot: Any = _OMITTED,
    repository_pr_config: Mapping[str, Any] | None | object = _OMITTED,
) -> TaskPolicyContext:
    """Take a fresh, detached snapshot of all policy-relevant session facts."""
    kernel = getattr(session_state, "bps_kernel", None) if bps_kernel is _OMITTED else bps_kernel
    if not isinstance(kernel, BpsKernel):
        kernel = BpsKernel("", "")
    return TaskPolicyContext(
        profile=profile, bps_kernel=kernel,
        anchor_root=getattr(session_state, "anchor_home", None),
        project_root=getattr(session_state, "project_root", None),
        artifact_root=getattr(session_state, "artifact_root", None),
        target_root=getattr(session_state, "target_root", None),
        target_worktree=getattr(session_state, "target_root", None),
        explicit_problem_requested=(getattr(session_state, "explicit_problem_requested", False)
                                    if explicit_problem_requested is _OMITTED else bool(explicit_problem_requested)),
        explicit_spec_requested=(getattr(session_state, "explicit_spec_requested", False)
                                 if explicit_spec_requested is _OMITTED else bool(explicit_spec_requested)),
        explicit_pr_draft_requested=(getattr(session_state, "explicit_pr_draft_requested", None)
                                     if explicit_pr_draft_requested is _OMITTED else explicit_pr_draft_requested),
        linked_problem=(getattr(session_state, "linked_problem", None)
                        if linked_problem is _OMITTED else linked_problem),
        linked_spec=(getattr(session_state, "linked_spec", None)
                     if linked_spec is _OMITTED else linked_spec),
        persisted_spec=(getattr(session_state, "spec_persisted", False)
                        if persisted_spec is _OMITTED else bool(persisted_spec)),
        linked_managed_artifacts=tuple(getattr(session_state, "managed_artifact_ledger", ())),
        current_phase=(getattr(session_state, "current_phase", 1) if current_phase is None else current_phase),
        phase_count=(getattr(session_state, "phase_count", 1) if phase_count is None else phase_count),
        current_action=current_action,
        current_effect=current_effect, current_effect_permissions=current_effect_permissions,
        referenced_facts=tuple(getattr(session_state, "referenced_facts", ())
                               if referenced_facts is _OMITTED else referenced_facts),
        caller_required_evidence=profile.caller_required_evidence,
        current_evidence_entries=tuple(getattr(session_state, "evidence_ledger", ())),
        guidance_attestations=tuple(getattr(session_state, "guidance_attestations", ())),
        task_verification_epoch=getattr(session_state, "task_verification_epoch", 0),
        checkpoint_final=checkpoint_final, source_files_changed=tuple(source_files_changed),
        observed_effects=tuple(getattr(session_state, "observed_effects", ())),
        intended_pr_paths=tuple(getattr(session_state, "intended_pr_paths", ())),
        active_runtime_identifiers={
            key: str(value) for key, value in {
                "session_id": getattr(session_state, "session_id", None),
                "project_id": getattr(session_state, "active_project", None),
            }.items() if value is not None
        },
        repository_snapshot=(getattr(session_state, "repository_snapshot", None)
                             if repository_snapshot is _OMITTED else repository_snapshot),
        repository_pr_config=(getattr(session_state, "repository_pr_config", None)
                              if repository_pr_config is _OMITTED else repository_pr_config),
    )


def evaluate_fresh_task_policies(
    profile: TaskProfile, *, session_state: Any, current_action: str,
    current_effect: ActionEffect = "orient",
    current_effect_permissions: frozenset[EffectPermission] | None = None,
    **facts: Any,
) -> tuple[TaskPolicyContext, TaskPolicySet]:
    """Build and evaluate one invocation snapshot; decisions are never cached."""
    if current_effect_permissions is None:
        from odibi_anchor._dispatcher._effects import EFFECT_PERMISSIONS
        current_effect_permissions = EFFECT_PERMISSIONS[current_effect]
    context = build_task_policy_context(
        profile, session_state=session_state, current_action=current_action,
        current_effect=current_effect,
        current_effect_permissions=current_effect_permissions, **facts,
    )
    return context, evaluate_task_policies(profile, context)
