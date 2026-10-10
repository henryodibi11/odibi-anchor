"""Versioned, transport-neutral result envelope for agent-facing dispatcher results.

Every dispatcher result that is a dict receives one additive key, ``envelope``.
Raised dispatcher exceptions carry the same structure as ``exc.envelope``, and the
CLI and MCP v2 transports copy it into their error object. Existing result keys are
never renamed or removed: a single nested key avoids collisions with action-specific
``status``, ``state``, ``outcome`` and ``next_operation`` fields.

The envelope answers, for one call: did it work (``outcome``), where am I (``state``),
what changed and was that re-read (``effects``), what degraded (``warnings``), what is
owed (``obligations``), what to do next (``next_operation``) and how to reverse it
(``undo``). ``verified_readback`` is true only when this module actually re-read the
mutated state.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

ENVELOPE_KEY = "envelope"
ENVELOPE_SCHEMA = "odibi_anchor.result_envelope"
ENVELOPE_VERSION = 1
OUTCOMES = ("succeeded", "succeeded_with_warnings", "blocked", "failed")
RESPONSE_DETAILS = frozenset({"compact", "full"})
MUTATING_EFFECTS = frozenset({
    "governance_write", "artifact_write", "source_write", "data_write", "external_mutation",
})
COMPACT_TASK_TOKEN_BUDGET = 1500

_TEXT_LIMIT = 300
_MAX_WARNINGS = 8
_MAX_OBLIGATIONS = 8
_IDEMPOTENT_ACTIONS = frozenset({"touched", "skill_loaded"})
_STATE_CHECKED_ACTIONS = frozenset({"workflow"})
_REPLAY_MARKERS = ("replayed", "deduped", "idempotent_replay")
_NEW_SESSION_RESETS = (
    "in_process_task_policy", "session_file_ledger", "skills_loaded",
    "session_timings_except_orientation",
)


def _text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    from odibi_anchor._dispatcher._request_adapter import redact_message

    text = redact_message(" ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _selector(action: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> str | None:
    if action in {"touched", "skill_loaded", "task", "new_session", "help"}:
        return None
    value = args[0] if args else kwargs.get("command")
    if isinstance(value, str) and value.strip() and len(value) <= 64:
        return value.strip().lower()
    return None


def is_mutating(effects: Iterable[str]) -> bool:
    """Return whether resolved invocation effects can change durable or runtime state."""
    return bool(MUTATING_EFFECTS.intersection(effects))


def retry_safety_for(action: str, effects: Iterable[str]) -> str:
    """Classify re-issuing one call: read_only, idempotent, state_checked or not_idempotent."""
    effects = tuple(effects)
    if not is_mutating(effects):
        return "read_only"
    if action in _IDEMPOTENT_ACTIONS:
        return "idempotent"
    if action in _STATE_CHECKED_ACTIONS:
        return "state_checked"
    return "not_idempotent"


def _static_effects(action: str) -> tuple[str, ...]:
    from odibi_anchor._dispatcher._effects import _FIXED_INVOCATIONS

    semantics = _FIXED_INVOCATIONS.get(action)
    return (semantics.effect,) if semantics is not None else ("governance_write",)


def resolve_effects(
    action: str, args: Sequence[Any], kwargs: Mapping[str, Any], *,
    contracts: Mapping[str, Any], registry: Any = None,
) -> tuple[str, ...]:
    """Resolve invocation effects with the dispatcher's own contracts, conservatively."""
    from odibi_anchor._dispatcher._effects import maximal_effects, resolve_invocation

    spec = registry.get_tool(action) if registry is not None else None
    if spec is not None:
        return tuple(sorted(spec.allowed_effects))
    contract = contracts.get(action)
    if contract is None:
        return ()
    resolution = resolve_invocation(contract, tuple(args), kwargs)
    if resolution.error is not None:
        return tuple(maximal_effects(contract.allowed_effects))
    return tuple(resolution.effects)


# ── State, obligations and next operation ────────────────────────────────────


def _protocol(result: Any, session_state: Any, action: str, timings: Sequence[Any]) -> dict[str, Any]:
    if isinstance(result, Mapping) and isinstance(result.get("operating_protocol"), Mapping):
        return dict(result["operating_protocol"])
    from odibi_anchor._dispatcher._operating_protocol import build_operating_protocol

    return build_operating_protocol(
        session_state, action=action,
        result=result if isinstance(result, Mapping) else None,
        session_timings=timings,
    )


def _obligations(protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in protocol.get("required_now", ()) or ():
        target = item.get("satisfy_with", {}) if isinstance(item, Mapping) else {}
        route = target.get("route")
        if target.get("skill"):
            route = f"{route}:{target['skill']}"
        rows.append({"id": str(item.get("id")), "status": "required", "route": route})
    # One obligation can appear as both required now and a pending verification;
    # report it once (as required) so open_obligations counts real debts.
    required = {row["id"] for row in rows}
    for item in protocol.get("verification_obligations", ()) or ():
        if (isinstance(item, Mapping) and item.get("status") == "pending"
                and str(item.get("id")) not in required):
            rows.append({"id": str(item.get("id")), "status": "pending", "route": None})
    return rows


def _workflow_state(action: str, result: Any, session_state: Any) -> dict[str, Any] | None:
    if action == "workflow" and isinstance(result, Mapping) and isinstance(result.get("state"), Mapping):
        state = result["state"]
        return {
            "workflow_id": state.get("workflow_id"), "phase": state.get("phase"),
            "progress": state.get("progress"), "generation": state.get("generation"),
        }
    binding = getattr(session_state, "workflow_binding", None)
    if isinstance(binding, Mapping) and binding.get("workflow_id"):
        return {"workflow_id": binding["workflow_id"], "phase": None, "progress": None, "generation": None}
    return None


def _state(action: str, result: Any, session_state: Any, protocol: Mapping[str, Any],
           obligations: Sequence[Any]) -> dict[str, Any]:
    profile = getattr(session_state, "active_task_profile", None)
    return {
        "project": getattr(session_state, "active_project", None),
        "target": getattr(session_state, "target_root", None),
        "task_window_id": getattr(session_state, "task_window_id", None) if profile is not None else None,
        "phase": protocol.get("phase"),
        "workflow": _workflow_state(action, result, session_state),
        "open_obligations": len(obligations),
    }


def _next_from(source: Any, *, default_requires_owner: bool = False) -> dict[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    copy_ready = source.get("copy_ready")
    if not isinstance(copy_ready, str) or not copy_ready.strip():
        return None
    action = source.get("action")
    retry = source.get("retry_safety")
    if not isinstance(retry, str):
        retry = retry_safety_for(str(action), _static_effects(str(action))) if action else "unknown"
    from odibi_anchor._dispatcher._request_adapter import redact_message

    return {
        "copy_ready": redact_message(copy_ready),
        "requires_owner": bool(source.get("requires_owner", default_requires_owner)),
        "retry_safety": retry,
        "reason": _text(source.get("reason") or ""),
    }


def _balanced_calls(message: str) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    start = message.find("anchor(")
    while start >= 0:
        depth, quote, end = 0, "", -1
        for index in range(start + len("anchor"), len(message)):
            char = message[index]
            if quote:
                quote = "" if char == quote and message[index - 1] != "\\" else quote
            elif char in "\"'":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end < 0:
            break
        calls.append((start, message[start:end]))
        start = message.find("anchor(", end)
    return calls


def _next_from_message(action: str, message: str) -> dict[str, Any] | None:
    """Recover the recovery ``anchor(...)`` call a legacy guard message names, if any.

    Guards that predate attach_recovery state their recovery call in prose, often after
    naming the refused call itself. Prefer a complete, balanced call introduced by
    "run"/"call"; never return the refused action. The result is labelled as such.
    """
    candidates = []
    for start, call in _balanced_calls(message):
        name = call[len("anchor("):].lstrip("\"'").split("\"", 1)[0].split("'", 1)[0]
        if name == action:
            continue
        lead = message[max(0, start - 12):start].lower()
        # An exact call beats a template such as anchor("skill_loaded", "<name>").
        placeholder = 1 if ("<" in call or "..." in call) else 0
        directed = 0 if ("run" in lead or "call" in lead or "register" in lead) else 1
        candidates.append((placeholder, directed, start, call, name))
    if not candidates:
        return None
    _placeholder, _directed, _start, call, name = min(candidates)
    return _next_from({
        "copy_ready": call, "action": name,
        "reason": "recovery call named by the blocking message",
    })


def _next_operation(result: Any, protocol: Mapping[str, Any]) -> dict[str, Any] | None:
    if isinstance(result, Mapping):
        for source in (result.get("next_operation"),
                       (result.get("agent_context") or {}).get("next_operation")
                       if isinstance(result.get("agent_context"), Mapping) else None):
            prepared = _next_from(source)
            if prepared is not None:
                return prepared
    from odibi_anchor._dispatcher._agent_context import _next_operation as derive

    return _next_from(derive(protocol))


# ── Warnings, outcome, effects, undo ─────────────────────────────────────────


def _warnings(action: str, result: Any, degraded: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = [
        {"code": "degraded", "component": str(row.get("component")),
         "message": _text(row.get("reason", "")), "fallback_used": True}
        for row in degraded
    ]
    if isinstance(result, Mapping):
        for flag in ("degraded", "fallback_used"):
            if result.get(flag):
                warnings.append({"code": flag, "message": _text(result[flag]), "fallback_used": True})
        if isinstance(result.get("warnings"), list):
            warnings.extend(
                {"code": "result_warning", "message": _text(item)} for item in result["warnings"][:3]
            )
        unavailable = result.get("unavailable_evidence")
        samples = result.get("samples")
        if not unavailable and isinstance(samples, Mapping):
            unavailable = samples.get("unavailable_evidence")
        if isinstance(unavailable, list) and unavailable:
            warnings.append({
                "code": "unavailable_evidence",
                "message": f"{len(unavailable)} evidence item(s) unavailable: "
                           + _text("; ".join(str(item) for item in unavailable[:3])),
            })
        readiness = result.get("readiness")
        if action == "task" and isinstance(readiness, Mapping) and readiness.get("status") not in {None, "ready"}:
            missing = readiness.get("missing_details") or []
            warnings.append({
                "code": "readiness_gap",
                "message": f"readiness {readiness.get('status')} ({readiness.get('score')}): "
                           + _text("; ".join(str(item) for item in missing[:3])),
            })
    return warnings[:_MAX_WARNINGS]


def _blocked_text(message: str) -> bool:
    return message.lstrip().upper().startswith("BLOCKED")


def _result_outcome(action: str, result: Any, warnings: Sequence[Any]) -> str:
    from odibi_anchor._dispatcher._effects import dispatch_succeeded

    if not dispatch_succeeded(action, result):
        if isinstance(result, Mapping) and (
            str(result.get("status", "")).lower() == "blocked" or result.get("blocked") is True
            or _blocked_text(str(result.get("error") or ""))
        ):
            return "blocked"
        return "failed"
    return "succeeded_with_warnings" if warnings else "succeeded"


def _invalidated(action: str, result: Any, before: Mapping[str, Any], session_state: Any) -> list[str]:
    items: list[str] = []
    if isinstance(result, Mapping):
        if isinstance(result.get("invalidated"), list):
            items.extend(str(item) for item in result["invalidated"][:10])
        if result.get("reinitialize_required") or result.get("routing_refreshed"):
            items.extend(["dispatcher_routing", "in_process_task_authority"])
    if action == "new_session":
        items.extend(_NEW_SESSION_RESETS)
        if before.get("task_window_id"):
            items.append(f"in_process_task_window:{before['task_window_id']}")
    if action == "task":
        prior = before.get("task_window_id")
        if prior and prior != getattr(session_state, "task_window_id", None):
            items.append(f"superseded_task_window:{prior}")
    return list(dict.fromkeys(items))


def _undo(action: str, selector: str | None, mutating: bool, before: Mapping[str, Any],
          session_state: Any) -> dict[str, Any]:
    if not mutating:
        return {"status": "not_applicable", "copy_ready": None, "reason": "no state was changed"}
    if action == "project" and selector == "use":
        previous = before.get("project")
        if previous and previous != getattr(session_state, "active_project", None):
            return {
                "status": "reversible",
                "copy_ready": f"anchor('project', 'use', {previous!r})",
                "reason": "restores the previous legacy interactive project selection",
            }
    reasons = {
        "touched": "registrations are append-only; revert the file content, then register it again",
        "skill_loaded": "skill registration is process bookkeeping and is not withdrawn",
        "new_session": "the previous in-process session state is not restored",
        "task": "task windows are append-only; close this one with anchor('learning', 'assess', ...) "
                "or anchor('learning', 'safe_stop', ...)",
        "workflow": "workflow transitions are append-only; recover with block, replan or cancel "
                    "at the current expected_generation",
    }
    return {
        "status": "irreversible", "copy_ready": None,
        "reason": reasons.get(action, "no supported reversal operation exists for this action"),
    }


# ── Readback probes (read-only; true only when state was actually re-read) ───


def _readback(action: str, args: Sequence[Any], result: Any, before: Mapping[str, Any],
              session_state: Any, context: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(result, Mapping):
        return None
    if action == "touched" and args:
        from odibi_anchor._utils._session_state import canonical_session_path

        ledger = context.get("files_changed")
        if ledger is None:
            return None
        root = context.get("root") or getattr(session_state, "target_root", "") or ""
        key = canonical_session_path(str(args[0]), str(root))
        return {"method": "session_file_ledger", "observed": key in ledger}
    if action == "skill_loaded":
        name = result.get("registered")
        return {"method": "session_skills_loaded",
                "observed": bool(name) and name in getattr(session_state, "skills_loaded", set())}
    if action == "new_session":
        current = getattr(session_state, "session_id", None)
        return {"method": "runtime_session_state",
                "observed": bool(current) and current != before.get("session_id")
                and getattr(session_state, "session_name", None) == result.get("subject")}
    if action == "task":
        window = getattr(session_state, "task_window_id", None)
        authority = result.get("accepted_task_authority")
        authority_window = authority.get("task_window_id") if isinstance(authority, Mapping) else None
        return {"method": "runtime_task_authority",
                "observed": bool(window) and window == result.get("task_window_id") == authority_window}
    if action == "workflow" and isinstance(result.get("state"), Mapping) and context.get("memory_db"):
        from odibi_anchor._dispatcher._workflow_admission import workflow_owner
        from odibi_anchor.codebase._workflow import read_workflow

        expected = result["state"]
        current = read_workflow(
            context["memory_db"], owner=workflow_owner(session_state),
            workflow_id=str(expected.get("workflow_id")),
        )
        return {"method": "durable_workflow_store",
                "observed": current.get("generation") == expected.get("generation")
                and current.get("progress") == expected.get("progress")}
    return None


def _effects_block(action: str, result: Any, effects: Sequence[str], *, failed: bool,
                   readback: Mapping[str, Any] | None, invalidated: list[str]) -> dict[str, Any]:
    if failed:
        changed: bool | None = None
    elif isinstance(result, Mapping) and (
        result.get("write_performed") is False
        or any(result.get(marker) is True for marker in _REPLAY_MARKERS)
    ):
        changed = False
    else:
        changed = True
    verified = bool(readback and readback.get("observed") is True and not failed)
    return {
        "changed": changed,
        "verified_readback": verified,
        "readback": dict(readback) if readback else None,
        "invalidated": invalidated,
        "effect_classes": sorted(MUTATING_EFFECTS.intersection(effects)),
    }


def _shell(action: str, selector: str | None) -> dict[str, Any]:
    return {"schema": ENVELOPE_SCHEMA, "version": ENVELOPE_VERSION, "action": action, "selector": selector}


def build_envelope(
    action: str, result: Any, *, args: Sequence[Any] = (), kwargs: Mapping[str, Any] | None = None,
    effects: Sequence[str] = (), session_state: Any = None, session_timings: Sequence[Any] = (),
    before: Mapping[str, Any] | None = None, degraded: Sequence[Mapping[str, Any]] = (),
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the v1 envelope for one completed dispatcher call."""
    kwargs = kwargs or {}
    before = before or {}
    context = context or {}
    selector = _selector(action, args, kwargs)
    warnings = _warnings(action, result, degraded)
    try:
        protocol = _protocol(result, session_state, action, session_timings)
    except Exception as exc:  # projection only; surface, never hide
        protocol = {}
        warnings.append({"code": "envelope_degraded", "component": "operating_protocol",
                         "message": _text(f"{type(exc).__name__}: {exc}"), "fallback_used": True})
    outcome = _result_outcome(action, result, warnings)
    failed = outcome in {"blocked", "failed"}
    mutating = is_mutating(effects)
    effects_block = None
    if mutating:
        try:
            readback = None if failed else _readback(action, args, result, before, session_state, context)
        except Exception as exc:
            readback = None
            warnings.append({"code": "degraded", "component": "envelope_readback",
                             "message": _text(f"{type(exc).__name__}: {exc}"), "fallback_used": True})
            if outcome == "succeeded":
                outcome = "succeeded_with_warnings"
        effects_block = _effects_block(
            action, result, effects, failed=failed, readback=readback,
            invalidated=[] if failed else _invalidated(action, result, before, session_state),
        )
    obligations = _obligations(protocol)
    try:
        next_operation = _next_operation(result, protocol)
    except Exception:
        next_operation = None
    return {
        **_shell(action, selector),
        "outcome": outcome,
        "state": _state(action, result, session_state, protocol, obligations),
        "effects": effects_block,
        "retry_safety": retry_safety_for(action, effects),
        "warnings": warnings[:_MAX_WARNINGS],
        "obligations": obligations[:_MAX_OBLIGATIONS],
        "next_operation": next_operation,
        "undo": _undo(action, selector, mutating and not failed, before, session_state),
        "error": None,
    }


def build_failure_envelope(
    action: str, exc: BaseException, *, args: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None, effects: Sequence[str] = (),
    session_state: Any = None, session_timings: Sequence[Any] = (),
    degraded: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the v1 envelope for a raised call, mapping attach_recovery metadata."""
    kwargs = kwargs or {}
    message = str(exc)
    blocked = _blocked_text(message) or getattr(exc, "requires_owner", False) is True
    warnings = _warnings(action, None, degraded)
    try:
        protocol = _protocol(None, session_state, action, session_timings)
    except Exception:
        protocol = {}
    obligations = _obligations(protocol)
    recovery = getattr(exc, "next_operation", None)
    next_operation = _next_from(recovery)
    if next_operation is not None and "retry_safety" not in recovery:
        next_operation["retry_safety"] = getattr(exc, "retry_safety", next_operation["retry_safety"])
    if next_operation is None:
        next_operation = _next_from_message(action, message)
    mutating = is_mutating(effects)
    error_code = getattr(exc, "error_code", None)
    return {
        **_shell(action, _selector(action, args, kwargs)),
        "outcome": "blocked" if blocked else "failed",
        "state": _state(action, None, session_state, protocol, obligations),
        "effects": _effects_block(action, None, effects, failed=True, readback=None, invalidated=[])
        if mutating else None,
        "retry_safety": retry_safety_for(action, effects),
        "warnings": warnings[:_MAX_WARNINGS],
        "obligations": obligations[:_MAX_OBLIGATIONS],
        "next_operation": next_operation,
        "undo": _undo(action, None, False, {}, session_state),
        "error": {
            "type": type(exc).__name__,
            "error_code": error_code if isinstance(error_code, str) else None,
            "message": _text(message.splitlines()[0] if message else type(exc).__name__),
        },
    }


def _minimal_envelope(action: str, outcome: str, exc: BaseException) -> dict[str, Any]:
    return {
        **_shell(action, None), "outcome": outcome,
        "state": None, "effects": None, "retry_safety": "unknown",
        "warnings": [{"code": "envelope_degraded", "message": _text(f"{type(exc).__name__}: {exc}"),
                      "fallback_used": True}],
        "obligations": [], "next_operation": None,
        "undo": {"status": "irreversible", "copy_ready": None, "reason": "envelope unavailable"},
        "error": None,
    }


# ── Dispatcher hook ──────────────────────────────────────────────────────────


def dispatch_with_envelope(
    core: Callable[..., Any], action: str, args: tuple[Any, ...], kwargs: dict[str, Any], *,
    session_state: Any, session_timings: Sequence[Any], contracts: Mapping[str, Any],
    registry: Any = None, files_changed: Any = None, root: str | None = None,
    memory_db: str | None = None,
) -> Any:
    """Run one core dispatcher call and attach the envelope to its result or exception."""
    detail = kwargs.pop("response_detail", "full")
    if detail not in RESPONSE_DETAILS:
        raise ValueError("response_detail must be 'compact' or 'full'")
    from odibi_anchor._utils._session_state import get_degraded

    try:
        effects = resolve_effects(action, args, kwargs, contracts=contracts, registry=registry)
    except Exception:
        effects = ()
    degraded_before = len(get_degraded())
    before = {
        "session_id": getattr(session_state, "session_id", None),
        "task_window_id": getattr(session_state, "task_window_id", None),
        "project": getattr(session_state, "active_project", None),
    }
    try:
        result = core(action, *args, **kwargs)
    except Exception as exc:
        try:
            envelope = build_failure_envelope(
                action, exc, args=args, kwargs=kwargs, effects=effects,
                session_state=session_state, session_timings=session_timings,
                degraded=get_degraded()[degraded_before:],
            )
        except Exception as build_error:
            envelope = _minimal_envelope(action, "failed", build_error)
        with contextlib.suppress(AttributeError, TypeError):
            exc.envelope = envelope  # type: ignore[attr-defined]
        raise
    if isinstance(result, dict):
        try:
            result[ENVELOPE_KEY] = build_envelope(
                action, result, args=args, kwargs=kwargs, effects=effects,
                session_state=session_state, session_timings=session_timings,
                before=before, degraded=get_degraded()[degraded_before:],
                context={"files_changed": files_changed, "root": root, "memory_db": memory_db},
            )
        except Exception as build_error:
            from odibi_anchor._dispatcher._effects import dispatch_succeeded

            result[ENVELOPE_KEY] = _minimal_envelope(
                action, "succeeded_with_warnings" if dispatch_succeeded(action, result) else "failed",
                build_error,
            )
        if detail == "compact":
            return compact_result(action, result)
    return result


def execute_with_envelope(dispatcher: Callable[..., Any], request: Any) -> dict[str, Any]:
    """Run ``execute_request`` and carry a raised call's envelope into its error object."""
    from odibi_anchor._dispatcher._request_adapter import execute_request

    captured: dict[str, Any] = {}

    def _capturing(action: str, /, *args: Any, **kwargs: Any) -> Any:
        # Positional-only: actions such as "convention" accept an ``action=`` keyword.
        try:
            return dispatcher(action, *args, **kwargs)
        except Exception as exc:
            captured["envelope"] = getattr(exc, "envelope", None)
            raise

    response = execute_request(_capturing, request)
    if not response["ok"] and isinstance(captured.get("envelope"), Mapping):
        response["error"]["envelope"] = captured["envelope"]
    return response


# ── Compact projections ──────────────────────────────────────────────────────


def _estimate_tokens(value: Any) -> int:
    from odibi_anchor._utils._output_hints import estimate_tokens

    return estimate_tokens(json.dumps(value, default=str, separators=(",", ":")))


def _clip_list(values: Any, limit: int, text_limit: int = 160) -> list[Any]:
    if not isinstance(values, list):
        return []
    return [_text(item, text_limit) if isinstance(item, str) else item for item in values[:limit]]


def _compact_memory(memory: Any) -> Any:
    if not isinstance(memory, Mapping):
        return memory
    selections = []
    for item in memory.get("selections", []) or []:
        if isinstance(item, Mapping):
            selections.append({
                key: (_text(item[key], 160) if isinstance(item[key], str) else item[key])
                for key in ("selection_id", "memory_id", "id", "summary", "provenance")
                if key in item
            })
    projected = {
        key: memory[key] for key in ("kind", "task_window_id", "selection_count", "authority")
        if key in memory
    }
    projected["selections"] = selections
    if memory.get("unavailable_evidence"):
        projected["unavailable_evidence"] = memory["unavailable_evidence"]
    return projected


def compact_task_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full task result to decision-critical fields within the token budget."""
    readiness = result.get("readiness") if isinstance(result.get("readiness"), Mapping) else {}
    profile = result.get("task_profile") if isinstance(result.get("task_profile"), Mapping) else {}
    agent_context = result.get("agent_context") if isinstance(result.get("agent_context"), Mapping) else {}
    workflow = result.get("workflow")
    authority = result.get("source_authority")
    compact: dict[str, Any] = {
        key: result[key] for key in ("kind", "version", "status", "mode", "task_window_id")
        if key in result
    }
    if "subject" in result:
        compact["subject"] = _text(result["subject"], 120)
    compact["readiness"] = {
        "status": readiness.get("status"), "score": readiness.get("score"),
        "missing_details": _clip_list(readiness.get("missing_details"), 3),
    }
    compact["task_profile"] = {
        key: profile[key] for key in ("work_type", "execution_mode", "risk", "rigor") if key in profile
    }
    compact["required_skills"] = [
        {key: item[key] for key in ("skill", "register") if key in item}
        if isinstance(item, Mapping) else item
        for item in (result.get("required_skills") or [])[:6]
    ]
    if isinstance(workflow, Mapping):
        # Bound packets keep identity under ``state``; unphased packets are top-level.
        state = workflow.get("state") if isinstance(workflow.get("state"), Mapping) else {}
        compact["workflow"] = {
            **{key: workflow[key] for key in ("status", "next_step") if key in workflow},
            **{key: state[key] for key in ("workflow_id", "status", "phase", "progress", "generation")
               if key in state},
        } or {"present": True}
    if isinstance(authority, Mapping):
        compact["source_authority"] = {
            key: _text(authority[key], 160) for key in ("status", "reason") if key in authority
        } or {"present": True}
    if "memory_context" in result:
        compact["memory_context"] = _compact_memory(result["memory_context"])
    if isinstance(agent_context.get("next_operation"), Mapping):
        compact["next_operation"] = {
            key: agent_context["next_operation"][key]
            for key in ("action", "copy_ready", "reason") if key in agent_context["next_operation"]
        }
    compact["suggested_next_actions"] = _clip_list(result.get("suggested_next_actions"), 3)
    if ENVELOPE_KEY in result:
        compact[ENVELOPE_KEY] = result[ENVELOPE_KEY]
    compact["transport"] = {
        "response_detail": "compact",
        "full_response_available": True,
        "full_response": 'pass response_detail="full" for every section',
        "omitted_sections": sorted(set(result) - set(compact)),
    }
    # Enforce the budget by dropping the least decision-critical fields first.
    for key in ("suggested_next_actions", "source_authority", "workflow"):
        if _estimate_tokens(compact) <= COMPACT_TASK_TOKEN_BUDGET:
            break
        compact.pop(key, None)
        compact["transport"]["omitted_sections"] = sorted(set(result) - set(compact) - {"transport"})
    return compact


def compact_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    """Apply the compact projection where one is defined; other actions are unchanged."""
    if action == "task":
        return compact_task_result(result)
    return result


# ── Operation envelopes for non-dispatcher CLI commands ──────────────────────


def build_operation_envelope(
    operation: str, result: Any, *, mutating: bool, readback: Mapping[str, Any] | None = None,
    undo: Mapping[str, Any] | None = None, invalidated: Sequence[str] = (),
) -> dict[str, Any]:
    """Envelope for CLI operations (state, portfolio) that run without a dispatcher."""
    action, _, selector = operation.partition(".")
    warnings = _warnings(operation, result, ())
    next_operation = None
    source = result.get("next_operation") if isinstance(result, Mapping) else None
    if isinstance(source, Mapping) and source.get("operation"):
        arguments = source.get("arguments") or {
            key: value for key, value in source.items() if key not in {"operation", "reason"}
        }
        rendered = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        next_operation = {
            "copy_ready": f"{source['operation']}({rendered})",
            "requires_owner": bool(source.get("requires_owner", False)),
            "retry_safety": str(source.get("retry_safety", "unknown")),
            "reason": _text(source.get("reason") or ""),
        }
    effects = None
    if mutating:
        effects = {
            "changed": not (isinstance(result, Mapping) and result.get("write_performed") is False),
            "verified_readback": bool(readback and readback.get("observed") is True),
            "readback": dict(readback) if readback else None,
            "invalidated": list(invalidated),
            "effect_classes": ["artifact_write"],
        }
    return {
        **_shell(action, selector or None),
        "outcome": "succeeded_with_warnings" if warnings else "succeeded",
        "state": {"project": None, "target": None, "task_window_id": None, "phase": None,
                  "workflow": None, "open_obligations": 0},
        "effects": effects,
        "retry_safety": "not_idempotent" if mutating else "read_only",
        "warnings": warnings,
        "obligations": [],
        "next_operation": next_operation,
        "undo": dict(undo) if undo else (
            {"status": "irreversible", "copy_ready": None,
             "reason": "no supported reversal operation exists for this command"}
            if mutating else
            {"status": "not_applicable", "copy_ready": None, "reason": "no state was changed"}
        ),
        "error": None,
    }


def build_operation_failure_envelope(operation: str, exc: BaseException, *, mutating: bool) -> dict[str, Any]:
    """Failure envelope for CLI operations, mapping attach_recovery metadata."""
    action, _, selector = operation.partition(".")
    message = str(exc)
    error_code = getattr(exc, "error_code", None)
    return {
        **_shell(action, selector or None),
        "outcome": "blocked" if _blocked_text(message) or getattr(exc, "requires_owner", False) is True
        else "failed",
        "state": {"project": None, "target": None, "task_window_id": None, "phase": None,
                  "workflow": None, "open_obligations": 0},
        "effects": {"changed": None, "verified_readback": False, "readback": None,
                    "invalidated": [], "effect_classes": ["artifact_write"]} if mutating else None,
        "retry_safety": "not_idempotent" if mutating else "read_only",
        "warnings": [],
        "obligations": [],
        "next_operation": _next_from(getattr(exc, "next_operation", None)),
        "undo": {"status": "not_applicable", "copy_ready": None, "reason": "the command did not complete"},
        "error": {
            "type": type(exc).__name__,
            "error_code": error_code if isinstance(error_code, str) else None,
            "message": _text(message.splitlines()[0] if message else type(exc).__name__),
        },
    }
