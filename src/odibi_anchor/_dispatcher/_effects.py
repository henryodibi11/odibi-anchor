"""Transport-neutral action effect contracts and overlap enforcement."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

ActionEffect = Literal[
    "orient", "read", "governance_write", "artifact_write", "source_write", "data_write",
    "external_mutation",
]
PreTaskAccess = Literal[
    "safe_orientation", "context_collection", "task_required",
]
EffectPermission = Literal[
    "read", "governance_write", "artifact_write", "source_write", "data_write",
    "external_mutation",
]

VALID_EFFECTS = frozenset({
    "orient", "read", "governance_write", "artifact_write", "source_write", "data_write",
    "external_mutation",
})
VALID_PRE_TASK_ACCESS = frozenset({
    "safe_orientation", "context_collection", "task_required",
})
EFFECT_PERMISSIONS: dict[ActionEffect, frozenset[EffectPermission]] = {
    "orient": frozenset(),
    "read": frozenset({"read"}),
    "governance_write": frozenset({"read", "governance_write"}),
    "artifact_write": frozenset({"read", "artifact_write"}),
    "source_write": frozenset({"read", "source_write"}),
    "data_write": frozenset({"read", "data_write"}),
    "external_mutation": frozenset({"external_mutation"}),
}


def dispatch_succeeded(action: str, result: Any, err: BaseException | None = None) -> bool:
    """Canonical action-aware success predicate for timing and mutations."""
    if err is not None:
        return False
    governed = {"gate", "checkpoint", "preflight", "test"}
    if action in governed and not isinstance(result, Mapping):
        return False
    if not isinstance(result, Mapping):
        return result is not None
    if result.get("error"):
        return False
    if result.get("passed") is False or result.get("overall_pass") is False:
        return False
    if action == "task":
        readiness = result.get("readiness")
        if isinstance(readiness, Mapping) and readiness.get("score", 100) < 40:
            return False
    failed = {"fail", "failed", "error", "blocked", "rejected"}
    if action == "reject":
        failed.remove("rejected")
    for container in (result, result.get("metrics")):
        if isinstance(container, Mapping):
            for key in ("status", "outcome", "result"):
                if str(container.get(key, "")).strip().lower() in failed:
                    return False
    metrics = result.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    if action == "checkpoint":
        return metrics.get("overall_pass") is True
    if action == "test":
        return metrics.get("exit_code") == 0
    if action == "preflight":
        if "passed" in result:
            return result.get("passed") is True
        if "overall_pass" in metrics:
            return metrics.get("overall_pass") is True
        return metrics.get("errors", 1) == 0
    if action == "gate":
        if "passed" in result:
            return result.get("passed") is True
        if "overall_pass" in metrics:
            return metrics.get("overall_pass") is True
        return str(metrics.get("risk_level", "")).lower() in {"none", "low", "medium"}
    return True


@dataclass(frozen=True)
class InvocationSemantics:
    """The effect and pre-task workflow class for one valid invocation."""

    effect: ActionEffect
    pre_task_access: PreTaskAccess


@dataclass(frozen=True)
class ActionContract:
    """Allowed behavior and the only invocation-sensitive resolver."""

    allowed_effects: frozenset[ActionEffect]
    allowed_pre_task_access: frozenset[PreTaskAccess]
    resolve_invocation: Callable[
        [tuple[Any, ...], Mapping[str, Any]], InvocationSemantics
    ] | None

    def __post_init__(self) -> None:
        if not self.allowed_effects or not self.allowed_effects.issubset(VALID_EFFECTS):
            raise ValueError("allowed_effects must be a non-empty set of valid effects")
        if (not self.allowed_pre_task_access
                or not self.allowed_pre_task_access.issubset(VALID_PRE_TASK_ACCESS)):
            raise ValueError(
                "allowed_pre_task_access must be a non-empty set of valid access values"
            )


@dataclass(frozen=True)
class InvocationResolution:
    """One resolution, or conservative behavior plus the resolver error."""

    effects: tuple[ActionEffect, ...]
    pre_task_access: PreTaskAccess
    error: Exception | None = None


@dataclass(frozen=True)
class EffectResolution:
    """Compatibility projection of an invocation resolution."""

    effects: tuple[ActionEffect, ...]
    error: Exception | None = None


def _fixed(
    effect: ActionEffect,
    pre_task_access: PreTaskAccess,
) -> Callable[[tuple[Any, ...], Mapping[str, Any]], InvocationSemantics]:
    semantics = InvocationSemantics(effect, pre_task_access)
    return lambda _args, _kwargs: semantics


def _selector(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    value = args[0] if args else kwargs.get("action")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("selector must be a string")
    return value.strip().lower()


def _learning(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
    selector = _selector(args, kwargs) or "list"
    if selector in {"list", "show", "insights", "export"}:
        return InvocationSemantics("read", "context_collection")
    if selector in {"capture", "assess", "safe_stop"}:
        return InvocationSemantics("governance_write", "task_required")
    if selector in {"triage", "backup"}:
        return InvocationSemantics("artifact_write", "task_required")
    raise ValueError(f"unknown learning selector: {selector!r}")


def _memory(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
    selector = _selector(args, kwargs)
    if selector in {"apply", "disposition", "evaluate"}:
        return InvocationSemantics("governance_write", "task_required")
    if selector == "recovery":
        command = str(kwargs.get("action", "inspect")).strip().lower()
        if command in {"declare_abandoned", "recover"}:
            return InvocationSemantics("governance_write", "task_required")
        if command == "inspect":
            return InvocationSemantics("read", "task_required")
        raise ValueError(f"unknown memory recovery action: {command!r}")
    if selector == "promotion":
        command = str(kwargs.get("command", "inspect")).strip().lower()
        if command in {
            "evaluate", "verify", "attest", "withdraw", "quarantine",
            "request_owner_activation", "request_owner_confirmation",
        }:
            return InvocationSemantics("artifact_write", "task_required")
        if command == "inspect":
            return InvocationSemantics("read", "context_collection")
        raise ValueError(f"unknown memory promotion command: {command!r}")
    if selector == "seed" and str(kwargs.get("command", "inspect")).strip().lower() == "load":
        return InvocationSemantics("artifact_write", "context_collection")
    if selector in {"diagnostics", "seed", "task_record", "replay", "storage"}:
        return InvocationSemantics("read", "context_collection")
    return InvocationSemantics("read", "context_collection")


def _concurrency(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
    positional = _selector(args, kwargs)
    keyword = kwargs.get("command")
    if positional and keyword is not None:
        raise ValueError("concurrency accepts one positional command or command=, not both")
    command = str(positional or keyword or "inspect").strip().lower()
    if command in {"inspect", "dry_run"}:
        return InvocationSemantics("read", "context_collection")
    if command in {"apply", "rollback"}:
        return InvocationSemantics("artifact_write", "task_required")
    raise ValueError(f"unknown concurrency command: {command!r}")


def _family(
    context: set[str],
    task_read: set[str],
    task_write: set[str],
) -> Callable[[tuple[Any, ...], Mapping[str, Any]], InvocationSemantics]:
    def resolve(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
        selector = _selector(args, kwargs)
        if selector in context:
            return InvocationSemantics("read", "context_collection")
        if selector in task_read:
            return InvocationSemantics("read", "task_required")
        if selector in task_write:
            return InvocationSemantics("artifact_write", "task_required")
        raise ValueError(f"unknown selector: {selector!r}")
    return resolve


def _is_safe_project_use(
    anchor_home: Path,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> bool:
    """Return whether exact existing-project selection performs no disk write."""
    if len(args) == 2 and "name" not in kwargs:
        candidate = args[1]
        allowed_kwargs = {"output_format"}
    elif len(args) == 1 and "name" in kwargs:
        candidate = kwargs["name"]
        allowed_kwargs = {"name", "output_format"}
    else:
        return False
    if set(kwargs) - allowed_kwargs:
        return False
    if kwargs.get("output_format", "dict") not in {"dict", "markdown"}:
        return False
    if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
        return False

    from odibi_anchor._dispatcher._project import (
        _list_projects,
        _normalize_project_id,
        _read_active_project_id,
        resolve_active_project,
    )

    projects = _list_projects(anchor_home)
    matches = [
        project for project in projects
        if candidate == project["id"] or candidate == project["name"]
    ]
    if len(matches) != 1:
        return False
    project_id = _normalize_project_id(candidate)
    if _normalize_project_id(matches[0]["id"]) != project_id:
        return False
    if _read_active_project_id(anchor_home) != project_id:
        return False
    resolved = resolve_active_project(anchor_home, project_id)
    if resolved is None:
        return False
    project_root = Path(resolved["project_root"])
    target_root = Path(resolved["target_root"])
    return (
        project_root.resolve() == Path(matches[0]["path"]).resolve()
        and project_root.is_dir()
        and target_root.is_dir()
    )


def _project(
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    anchor_home: Path,
) -> InvocationSemantics:
    selector = _selector(args, kwargs)
    if selector in {"", "list", "status"}:
        return InvocationSemantics("read", "safe_orientation")
    if selector == "use":
        if _is_safe_project_use(anchor_home, args, kwargs):
            return InvocationSemantics("read", "safe_orientation")
        return InvocationSemantics("artifact_write", "task_required")
    if selector in {"create", "set_target"}:
        return InvocationSemantics("artifact_write", "task_required")
    if selector == "migrate":
        dry_run = kwargs.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be boolean")
        return InvocationSemantics(
            "read" if dry_run else "artifact_write", "task_required",
        )
    raise ValueError(f"unknown project selector: {selector!r}")


def _config(_args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
    mutation = {
        "suppress_category", "unsuppress_category", "suppress_id", "unsuppress_id",
        "file_override", "remove_override",
    }
    if mutation.intersection(kwargs):
        return InvocationSemantics("artifact_write", "task_required")
    return InvocationSemantics("read", "context_collection")


def _contract(_args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
    save = kwargs.get("save", False)
    if not isinstance(save, bool):
        raise ValueError("save must be boolean")
    return InvocationSemantics(
        "artifact_write" if save else "read", "task_required",
    )


def _apply_sql(_args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
    mode = kwargs.get("mode", "view")
    if mode not in {"view", "table"}:
        raise ValueError("apply_sql mode must be 'view' or 'table'")
    # Both modes execute caller-supplied SQL through spark.sql(). A statement named
    # as a view operation can still contain DDL/DML, so invocation mode is not a
    # reliable permission boundary.
    return InvocationSemantics("data_write", "task_required")


def _boolean_invocation(
    name: str,
    default: bool,
    false_effect: ActionEffect,
    true_effect: ActionEffect,
):
    def resolve(_args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> InvocationSemantics:
        value = kwargs.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be boolean")
        return InvocationSemantics(
            true_effect if value else false_effect, "task_required",
        )
    return resolve


BUILTIN_ACTION_NAMES = frozenset({
    "memory", "context", "prepare", "concurrency", "map", "impact", "consistency", "convention", "safe", "semantic",
    "import_resolve", "known_bad", "task", "task_rebind", "task_adoption", "gate", "preflight", "test", "checkpoint",
    "spec", "problem", "work_item", "reconcile", "investigate", "debug", "trace_row", "evolve",
    "incident_snapshot", "environment_diff", "spark_diagnose", "uc_context", "delta_changes",
    "run_diff", "observe_table", "table_trend",
    "chain", "profile_table", "microscope", "case_file", "quality", "validate",
    "duplicate", "diff", "schema_diff", "contract", "transform", "apply_transform",
    "apply_sql", "rollback", "unpersist", "coerce_check", "known_error", "trace",
    "lookup", "learn", "learning", "save", "confirm", "reject", "snapshot", "save_snap",
    "load_snap", "archive", "export_md", "import_md", "dogfood", "db_migrate",
    "memory_hygiene", "memory_stats", "session_files", "session_diff", "session_delta",
    "review", "status", "memory_tags", "audit_history", "manifest", "config", "project",
    "skills", "references", "tools", "register_tool", "frame", "touched", "skill_loaded", "log",
    "session_log", "help", "sync", "new_session", "orient", "quick",
})


_FIXED_INVOCATIONS: dict[str, InvocationSemantics] = {
    "archive": InvocationSemantics("artifact_write", "task_required"),
    "audit_history": InvocationSemantics("read", "safe_orientation"),
    "case_file": InvocationSemantics("read", "task_required"),
    "chain": InvocationSemantics("read", "task_required"),
    "checkpoint": InvocationSemantics("artifact_write", "task_required"),
    "coerce_check": InvocationSemantics("read", "task_required"),
    "confirm": InvocationSemantics("artifact_write", "task_required"),
    "consistency": InvocationSemantics("read", "task_required"),
    "context": InvocationSemantics("read", "safe_orientation"),
    "convention": InvocationSemantics("read", "context_collection"),
    "db_migrate": InvocationSemantics("artifact_write", "task_required"),
    "debug": InvocationSemantics("read", "task_required"),
    "delta_changes": InvocationSemantics("read", "task_required"),
    "diff": InvocationSemantics("read", "task_required"),
    "duplicate": InvocationSemantics("read", "task_required"),
    "environment_diff": InvocationSemantics("read", "task_required"),
    "evolve": InvocationSemantics("read", "task_required"),
    "export_md": InvocationSemantics("read", "task_required"),
    "frame": InvocationSemantics("read", "context_collection"),
    "gate": InvocationSemantics("read", "task_required"),
    "help": InvocationSemantics("orient", "safe_orientation"),
    "impact": InvocationSemantics("read", "task_required"),
    "import_md": InvocationSemantics("artifact_write", "task_required"),
    "import_resolve": InvocationSemantics("read", "context_collection"),
    "incident_snapshot": InvocationSemantics("artifact_write", "task_required"),
    "investigate": InvocationSemantics("read", "task_required"),
    "known_bad": InvocationSemantics("read", "task_required"),
    "known_error": InvocationSemantics("read", "task_required"),
    "learn": InvocationSemantics("artifact_write", "task_required"),
    "load_snap": InvocationSemantics("read", "task_required"),
    "log": InvocationSemantics("artifact_write", "task_required"),
    "lookup": InvocationSemantics("read", "context_collection"),
    "manifest": InvocationSemantics("read", "context_collection"),
    "map": InvocationSemantics("read", "context_collection"),
    "memory_stats": InvocationSemantics("read", "context_collection"),
    "memory_tags": InvocationSemantics("read", "context_collection"),
    "microscope": InvocationSemantics("read", "task_required"),
    "new_session": InvocationSemantics("artifact_write", "safe_orientation"),
    "orient": InvocationSemantics("orient", "safe_orientation"),
    "preflight": InvocationSemantics("read", "task_required"),
    "prepare": InvocationSemantics("read", "safe_orientation"),
    "profile_table": InvocationSemantics("read", "task_required"),
    "quality": InvocationSemantics("read", "task_required"),
    "quick": InvocationSemantics("orient", "safe_orientation"),
    "reconcile": InvocationSemantics("read", "task_required"),
    "register_tool": InvocationSemantics("artifact_write", "task_required"),
    "reject": InvocationSemantics("artifact_write", "task_required"),
    "review": InvocationSemantics("read", "task_required"),
    "rollback": InvocationSemantics("read", "task_required"),
    "run_diff": InvocationSemantics("read", "task_required"),
    "safe": InvocationSemantics("source_write", "task_required"),
    "save": InvocationSemantics("artifact_write", "task_required"),
    "save_snap": InvocationSemantics("artifact_write", "task_required"),
    "schema_diff": InvocationSemantics("read", "task_required"),
    "semantic": InvocationSemantics("source_write", "task_required"),
    "session_delta": InvocationSemantics("read", "context_collection"),
    "session_diff": InvocationSemantics("read", "context_collection"),
    "session_files": InvocationSemantics("read", "context_collection"),
    "session_log": InvocationSemantics("read", "context_collection"),
    "skill_loaded": InvocationSemantics("artifact_write", "safe_orientation"),
    "skills": InvocationSemantics("read", "safe_orientation"),
    "references": InvocationSemantics("read", "safe_orientation"),
    "snapshot": InvocationSemantics("artifact_write", "task_required"),
    "spark_diagnose": InvocationSemantics("read", "task_required"),
    "status": InvocationSemantics("read", "safe_orientation"),
    "sync": InvocationSemantics("external_mutation", "task_required"),
    "table_trend": InvocationSemantics("read", "task_required"),
    "task": InvocationSemantics("artifact_write", "safe_orientation"),
    "task_adoption": InvocationSemantics("artifact_write", "safe_orientation"),
    "task_rebind": InvocationSemantics("artifact_write", "context_collection"),
    "test": InvocationSemantics("read", "task_required"),
    "tools": InvocationSemantics("read", "safe_orientation"),
    "touched": InvocationSemantics("artifact_write", "task_required"),
    "trace": InvocationSemantics("read", "task_required"),
    "trace_row": InvocationSemantics("read", "task_required"),
    "transform": InvocationSemantics("read", "task_required"),
    "uc_context": InvocationSemantics("read", "task_required"),
    "unpersist": InvocationSemantics("read", "task_required"),
    "validate": InvocationSemantics("read", "task_required"),
}


def build_static_action_contracts(
    *,
    anchor_home: str | Path | None = None,
) -> dict[str, ActionContract]:
    """Build exhaustive invocation contracts for the canonical built-in surface."""
    if anchor_home is None:
        from odibi_anchor._dispatcher._boot import ANCHOR_ROOT

        project_home = Path(ANCHOR_ROOT).expanduser().resolve()
    else:
        project_home = Path(anchor_home).expanduser().resolve()
    contracts = {
        name: ActionContract(
            frozenset({semantics.effect}),
            frozenset({semantics.pre_task_access}),
            _fixed(semantics.effect, semantics.pre_task_access),
        )
        for name, semantics in _FIXED_INVOCATIONS.items()
    }
    contracts["learning"] = ActionContract(
        frozenset({"read", "governance_write", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _learning,
    )
    contracts["memory"] = ActionContract(
        frozenset({"read", "governance_write", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _memory,
    )
    contracts["concurrency"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _concurrency,
    )
    contracts["problem"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _family(
            {"", "list", "status", "show", "resume"},
            set(),
            {"create", "update", "link_spec", "close"},
        ),
    )
    contracts["spec"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _family(
            {"", "list", "status"},
            {"validate"},
            {"create", "done", "persist", "execute", "review", "from_problem"},
        ),
    )
    contracts["work_item"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _family(
            {"", "list", "status", "show"},
            {"preview"},
            {"create", "update", "approve", "record_publish", "close"},
        ),
    )
    contracts["project"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset(VALID_PRE_TASK_ACCESS),
        lambda args, kwargs: _project(args, kwargs, anchor_home=project_home),
    )
    contracts["config"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"context_collection", "task_required"}),
        _config,
    )
    contracts["contract"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"task_required"}),
        _contract,
    )
    contracts["apply_sql"] = ActionContract(
        frozenset({"data_write"}),
        frozenset({"task_required"}),
        _apply_sql,
    )
    contracts["apply_transform"] = ActionContract(
        frozenset({"read", "data_write"}),
        frozenset({"task_required"}),
        _boolean_invocation("dry_run", False, "data_write", "read"),
    )
    contracts["dogfood"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"task_required"}),
        _boolean_invocation("save_as_baseline", False, "read", "artifact_write"),
    )
    contracts["memory_hygiene"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"task_required"}),
        _boolean_invocation("dry_run", True, "artifact_write", "read"),
    )
    contracts["observe_table"] = ActionContract(
        frozenset({"read", "artifact_write"}),
        frozenset({"task_required"}),
        _boolean_invocation("persist", False, "read", "artifact_write"),
    )
    if set(contracts) != set(BUILTIN_ACTION_NAMES):
        missing = sorted(set(BUILTIN_ACTION_NAMES) - set(contracts))
        extra = sorted(set(contracts) - set(BUILTIN_ACTION_NAMES))
        raise AssertionError(
            f"built-in invocation classification drift: missing={missing}, extra={extra}"
        )
    return contracts


def maximal_effects(effects: frozenset[ActionEffect]) -> tuple[ActionEffect, ...]:
    """Return all inclusion-maximal effects without inventing a union effect."""
    return tuple(sorted(
        effect for effect in effects
        if not any(EFFECT_PERMISSIONS[effect] < EFFECT_PERMISSIONS[other] for other in effects)
    ))


def resolve_invocation(
    contract: ActionContract,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> InvocationResolution:
    """Invoke the sole behavior resolver once and fail closed on any invalid result."""
    try:
        if contract.resolve_invocation is None:
            raise ValueError("missing resolver")
        semantics = contract.resolve_invocation(args, kwargs)
        semantics_type = type(semantics)
        same_semantics_type = (
            semantics_type.__module__ == InvocationSemantics.__module__
            and semantics_type.__qualname__ == InvocationSemantics.__qualname__
        )
        if not isinstance(semantics, InvocationSemantics) and not same_semantics_type:
            raise ValueError("resolver returned invalid invocation semantics")
        if semantics.effect not in contract.allowed_effects:
            raise ValueError("resolver returned an effect outside allowed_effects")
        if semantics.pre_task_access not in contract.allowed_pre_task_access:
            raise ValueError(
                "resolver returned pre-task access outside allowed_pre_task_access"
            )
        return InvocationResolution(
            (cast(ActionEffect, semantics.effect),),
            cast(PreTaskAccess, semantics.pre_task_access),
        )
    except Exception as exc:
        return InvocationResolution(
            maximal_effects(contract.allowed_effects), "task_required", exc,
        )


def resolve_effects(
    contract: ActionContract,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> EffectResolution:
    """Compatibility projection for focused callers outside production dispatch."""
    resolution = resolve_invocation(contract, args, kwargs)
    return EffectResolution(resolution.effects, resolution.error)


_PRE_PROFILE_BOOKKEEPING = frozenset({"task", "task_adoption", "task_rebind", "new_session"})
_ACTIVE_PROFILE_BOOKKEEPING = frozenset({"checkpoint", "learn", "skill_loaded", "log"})


def task_profile_effect_compatible(effect: ActionEffect, profile: Any | None) -> bool:
    """Return task-profile compatibility, without granting invocation authority."""
    if profile is None or effect == "external_mutation":
        return False
    if effect in {"orient", "read"}:
        return True
    if effect == "governance_write":
        return True
    mode = profile.execution_mode
    if effect == "artifact_write":
        return mode in {"artifact_only", "source_change", "data_change"}
    traits = profile.traits
    dual = {"source-change", "data-change"}.issubset(traits)
    if effect == "source_write":
        return mode == "source_change" or (dual and mode == "data_change")
    if effect == "data_write":
        return mode == "data_change" or (dual and mode == "source_change")
    return False


def enforce_effects(action: str, effects: tuple[ActionEffect, ...], profile: Any | None) -> None:
    """Gate every resolved/maximal permission set; legacy gates still run separately."""
    for effect in effects:
        if effect == "external_mutation":
            raise RuntimeError("BLOCKED: external mutation is not authorized by Phase 1")
        if effect in {"orient", "read"}:
            continue
        if effect == "artifact_write" and action in _PRE_PROFILE_BOOKKEEPING:
            continue
        if profile is None:
            raise RuntimeError(f"BLOCKED: {effect} requires an active task profile")
        if effect == "artifact_write" and action in _ACTIVE_PROFILE_BOOKKEEPING:
            continue
        if task_profile_effect_compatible(effect, profile):
            continue
        mode = profile.execution_mode
        raise RuntimeError(f"BLOCKED: {effect} is incompatible with task execution_mode {mode!r}")
