"""MCP server for odibi_anchor — exposes anchor() tools via FastMCP.

Usage:
    python -m odibi_anchor.mcp_server          # stdio transport (default)
    fastmcp run odibi_anchor.mcp_server:mcp    # or via fastmcp CLI

Environment variables:
    ANCHOR_PROJECT_ID    — explicit managed project ID (preferred)
    ANCHOR_PROJECT_ROOT  — target-root hint used for exact registry discovery
    ANCHOR_ALLOW_LEGACY_SELECTOR — set to ``1`` only for legacy selector fallback
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypeAlias

# Force UTF-8 on Windows so Unicode symbols (✓, ✗, ▶, …) survive stdio transport
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from fastmcp import FastMCP
from pydantic import BeforeValidator, StrictInt

from odibi_anchor._dispatcher._dispatch_table import ACTION_GROUPS, DISPATCH_SIGS
from odibi_anchor._dispatcher._request_adapter import (
    decode_json_document,
    error_information,
    execute_request,
    normalize_request,
)
from odibi_anchor._utils._output_hints import estimate_tokens

# ─── Server instance ─────────────────────────────────────────────────────────
mcp = FastMCP("odibi-anchor")

# ─── Bootstrap anchor() dispatcher ───────────────────────────────────────────────
_CW = None
_ROOT = None
_ROUTE_BINDING = None
_GATEWAY_LOCK = threading.RLock()

_COMPACT_TASK_KEYS = (
    "kind", "version", "subject", "summary", "status", "mode", "readiness",
    "intent", "background", "scope", "resources", "context", "constraints",
    "context_plan", "verification", "guardrails", "risks", "findings",
    "required_skills", "suggested_next_actions", "task_profile", "work_item_policy",
    "artifact_contract", "capture_guidance", "operating_protocol", "agent_context",
)

_TELEMETRY_STAGES = (
    "gateway_lock_wait", "bootstrap", "normalization_resolution", "dispatch",
    "response_preparation", "serialization", "connection_cleanup",
)


def _validate_response_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
        raise ValueError("response_version must be 1 or 2")
    return value


def _validate_response_detail(value: Any) -> str:
    if not isinstance(value, str) or value not in {"compact", "full"}:
        raise ValueError("response_detail must be 'compact' or 'full'")
    return value


def _validate_telemetry_version(value: Any) -> int | None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value != 1
    ):
        raise ValueError("telemetry_version must be 1 or None")
    return value


# FastMCP 4 validates with ``strict=False``, which overrides ``StrictInt``.
# Before-validation preserves the raw provider input; direct calls reuse these helpers below.
_ResponseVersion: TypeAlias = Annotated[StrictInt, BeforeValidator(_validate_response_version)]
_ResponseDetail: TypeAlias = Annotated[str, BeforeValidator(_validate_response_detail)]
_TelemetryVersion: TypeAlias = Annotated[StrictInt, BeforeValidator(_validate_telemetry_version)]


@dataclass
class _McpTelemetry:
    """Bounded, request-local MCP server timing state."""

    started_ns: int
    stages_ms: dict[str, float | None] = field(
        default_factory=lambda: dict.fromkeys(_TELEMETRY_STAGES),
    )
    routing_status: str = "not_needed"
    routing_elapsed_ms: float | None = None

    def elapsed_ms(self, started_ns: int) -> float:
        return round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)

    @contextlib.contextmanager
    def stage(self, name: str):
        started_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            self.stages_ms[name] = self.elapsed_ms(started_ns)


def _close_memory_connections() -> None:
    """Release SQLite handles between persistent MCP requests."""
    from odibi_anchor.codebase._memory_db import close_all_dbs

    close_all_dbs()


def _legacy_selector_enabled() -> bool:
    """Return the explicitly requested server compatibility mode."""
    value = os.environ.get("ANCHOR_ALLOW_LEGACY_SELECTOR")
    if value is None:
        return False
    if value != "1":
        raise RuntimeError("ANCHOR_ALLOW_LEGACY_SELECTOR must be '1' when enabled")
    return True


def _resolve_server_binding(anchor_home: str, target_hint: str | None):
    """Resolve one immutable process binding before constructing the dispatcher."""
    from odibi_anchor._dispatcher._project import resolve_route_binding

    runtime_id = os.environ.get("ANCHOR_RUNTIME_INSTANCE_ID") or f"mcp:{uuid.uuid4()}"
    binding = resolve_route_binding(
        anchor_home,
        project=os.environ.get("ANCHOR_PROJECT_ID"),
        target_hint=target_hint,
        allow_legacy_selector=_legacy_selector_enabled(),
        runtime_instance_id=runtime_id,
    )
    if binding is None:
        raise RuntimeError(
            "MCP server requires ANCHOR_PROJECT_ID, a uniquely registered ANCHOR_PROJECT_ROOT, "
            "or explicit ANCHOR_ALLOW_LEGACY_SELECTOR=1 with a remembered project"
        )
    return binding


def _initialize_dispatcher(*, reset: bool) -> Any:
    """Initialize MCP routing from the currently selected Odibi Anchor project.

    Args:
        reset: Whether to clear the prior process session before initialization.

    Returns:
        Newly initialized Odibi Anchor dispatcher.
    """
    global _CW, _ROOT, _ROUTE_BINDING

    from odibi_anchor._dispatcher._boot import (
        _installed_boot_authority,
        resolve_boot_environment,
        resolve_installed_project_bootstrap,
    )
    from odibi_anchor._utils._session_state import (
        reset_session_state,
        uninstall_write_guard,
    )

    if reset:
        reset_session_state()
    # Release process-global resources before init() evicts package modules.
    _close_memory_connections()
    uninstall_write_guard()
    _CW = None
    _ROOT = None
    _table_cache.clear()

    from odibi_anchor.bootstrap import init

    project_root = os.environ.get("ANCHOR_PROJECT_ROOT")
    boot_environment = resolve_boot_environment()
    runtime_paths = boot_environment["runtime_paths"]
    installed_bootstrap = None
    if not runtime_paths.source_checkout and project_root is not None:
        installed_bootstrap = resolve_installed_project_bootstrap(project_root, os.environ)
        installed_bootstrap.runtime_paths.anchor_home.mkdir(parents=True, exist_ok=True)
        installed_bootstrap.memory_db.parent.mkdir(parents=True, exist_ok=True)
        anchor_home = str(installed_bootstrap.runtime_paths.anchor_home)
        target_hint = str(installed_bootstrap.canonical_target)
    else:
        anchor_home = str(runtime_paths.anchor_home)
        target_hint = project_root
    if _ROUTE_BINDING is None:
        _ROUTE_BINDING = _resolve_server_binding(anchor_home, target_hint)
    restored_environment: dict[str, str | None] = {}
    if installed_bootstrap is not None:
        validated_environment = {
            "ANCHOR_PROJECT_ROOT": str(installed_bootstrap.canonical_target),
            "ANCHOR_HOME": str(installed_bootstrap.runtime_paths.anchor_home),
            "ANCHOR_MEMORY_DB": str(installed_bootstrap.memory_db),
            "_CW_BOOT_CONFIG_ROOT": str(installed_bootstrap.canonical_target),
        }
        restored_environment = {
            name: os.environ.get(name) for name in validated_environment
        }
        os.environ.update(validated_environment)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            candidate_cw, candidate_root, _ = init(
                route_binding=_ROUTE_BINDING,
                output_format="dict",
            )
        if installed_bootstrap is not None:
            runtime = candidate_cw("status", output_format="dict")["runtime"]
            from odibi_anchor._dispatcher._boot import _ENV as effective_environment

            expected_home = installed_bootstrap.runtime_paths.anchor_home
            expected_artifact_root = Path(_ROUTE_BINDING.artifact_root)
            effective_runtime_paths = effective_environment["runtime_paths"]
            if (
                Path(candidate_root).resolve(strict=False) != installed_bootstrap.canonical_target
                or Path(runtime["artifact_root"]).resolve(strict=False)
                != expected_artifact_root
                or Path(runtime["target_root"]).resolve(strict=False)
                != installed_bootstrap.canonical_target
                or effective_runtime_paths.source_checkout
                != installed_bootstrap.runtime_paths.source_checkout
                or Path(effective_runtime_paths.resource_root).resolve(strict=False)
                != installed_bootstrap.runtime_paths.resource_root
                or Path(effective_runtime_paths.instructions_file).resolve(strict=False)
                != installed_bootstrap.runtime_paths.instructions_file
                or Path(effective_runtime_paths.skills_dir).resolve(strict=False)
                != installed_bootstrap.runtime_paths.skills_dir
                or Path(effective_runtime_paths.tools_dir).resolve(strict=False)
                != installed_bootstrap.runtime_paths.tools_dir
                or Path(effective_runtime_paths.anchor_home).resolve(strict=False) != expected_home
                or Path(effective_environment["skills_dir"]).resolve(strict=False)
                != installed_bootstrap.runtime_paths.skills_dir
                or Path(effective_environment["tools_dir"]).resolve(strict=False)
                != installed_bootstrap.runtime_paths.tools_dir
                or Path(effective_environment["memory_db"]).expanduser().resolve(strict=False)
                != installed_bootstrap.memory_db
                or _installed_boot_authority(effective_environment)
                != installed_bootstrap.authority
            ):
                raise RuntimeError(
                    "installed orb bootstrap effective runtime does not match validated paths"
                )
        _CW, _ROOT = candidate_cw, candidate_root
    except Exception:
        _CW = None
        _ROOT = None
        _close_memory_connections()
        raise
    finally:
        for name, value in restored_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    _close_memory_connections()
    return _CW


def _boot() -> Any:
    """Lazily bootstrap the anchor dispatcher on first tool call.

    The anchor *bootstrap* prints a banner to stdout. On the stdio MCP transport
    stdout IS the JSON-RPC channel, so we redirect that banner to stderr during
    init(). We do NOT wrap every anchor() call in redirect_stdout: that globally
    swaps sys.stdout for the duration of the call, which races/deadlocks with
    FastMCP's stdio transport writing the response (manifests as the tool call
    hanging with no reply). anchor() actions return data rather than printing, so the
    one-time boot redirect is sufficient to keep the protocol stream clean.
    """
    if _CW is None:
        # A server process may be reused after an in-process client/test. Reset
        # exactly once at MCP boot; subsequent calls intentionally share a
        # session until anchor("new_session") rotates its identity.
        _initialize_dispatcher(reset=True)
    return _CW


# ─── DataFrame resolver ─────────────────────────────────────────────────────
_table_cache: dict[str, Any] = {}


def _resolve(table_ref: str) -> Any:
    """Resolve a table reference to a DataFrame.

    Resolution order:
        1. In-memory cache (_table_cache)
        2. Pandas reader (csv / parquet / json) when the ref has a data-file
           extension — checked FIRST so file paths never pay the ~2s pyspark
           import cost just because the path contains a dot.
        3. Spark catalog (extensionless ``catalog.schema.table`` refs, if a
           SparkSession is active).
    """
    if table_ref in _table_cache:
        return _table_cache[table_ref]

    df: Any = None
    ext = os.path.splitext(table_ref)[1].lower()

    # File path — read with pandas directly (no pyspark import).
    if ext in (".csv", ".parquet", ".json"):
        import pandas as pd

        readers = {
            ".csv": pd.read_csv,
            ".parquet": pd.read_parquet,
            ".json": pd.read_json,
        }
        df = readers[ext](table_ref)

    # Otherwise a dotted catalog ref → Spark catalog (lazy pyspark import).
    elif "." in table_ref:
        try:
            from pyspark.sql import SparkSession

            spark = SparkSession.getActiveSession()
            if spark is not None:
                df = spark.table(table_ref)
        except Exception:
            pass

    if df is None:
        raise ValueError(
            f"Cannot resolve '{table_ref}': not in cache, extension "
            f"'{ext}' is not csv/parquet/json, and no active Spark table matches."
        )

    _table_cache[table_ref] = df
    return df


def _resolve_or_passthrough(table_ref: str) -> Any:
    """Resolve a file-path ref (csv/parquet/json) to a DataFrame; pass any other
    ref (e.g. a Delta ``catalog.schema.table`` name) through unchanged.

    Used for merge/upsert targets: a file target should be read into a DataFrame
    (so file-based use and testing work without a SparkSession), while a real
    Delta table name must reach the tool as a string so it reads via Spark.
    """
    ext = os.path.splitext(table_ref)[1].lower()
    if ext in (".csv", ".parquet", ".json"):
        return _resolve(table_ref)
    return table_ref


def _resolve_upstreams(mapping: dict[str, str]) -> dict[str, Any]:
    """Resolve a dict of name → table_ref into name → DataFrame."""
    return {name: _resolve(ref) for name, ref in mapping.items()}


_TABLE_ARGUMENTS = {
    "table", "df", "old_df", "new_df", "result_df", "output_df",
    "source_df", "target_df", "left_df", "right_df",
}


def _resolve_request_value(value: Any, location: str) -> Any:
    """Preserve the MCP gateway's positional, table, target, and upstream resolution."""
    if location.startswith("args[") and isinstance(value, str):
        try:
            return _resolve(value)
        except ValueError:
            return value
    key = location.removeprefix("kwargs.")
    if key in _TABLE_ARGUMENTS and isinstance(value, str):
        try:
            return _resolve(value)
        except ValueError:
            return value
    if key == "target" and isinstance(value, str):
        try:
            return _resolve_or_passthrough(value)
        except ValueError:
            return value
    if key in {"upstream", "upstreams"} and isinstance(value, dict):
        try:
            return _resolve_upstreams(value)
        except ValueError:
            return value
    return value


def _fmt(result: Any) -> str:
    """Format a anchor() result for MCP text output.

    Compact JSON (no indent): the consumer is an LLM, so pretty-print whitespace
    is pure token cost — it roughly doubled the payload (a composed workflow blew
    past the response limit on whitespace alone) and made the engine's
    ``output_tokens_estimate`` (computed on compact JSON) read ~2x low versus the
    transported string. Compact output keeps the two in agreement.
    """
    if isinstance(result, dict):
        return json.dumps(result, default=str, separators=(",", ":"))
    return str(result)


def _passthrough(kwargs: dict[str, Any], **named: Any) -> dict[str, Any]:
    """Build kwargs dict from named args, skipping None values."""
    for key, val in named.items():
        if val is not None:
            kwargs[key] = val
    return kwargs


def _refresh_after_project_change(
    action: str, result: Any, telemetry: _McpTelemetry | None = None,
) -> Any:
    """Refresh MCP-local routing after a successful project mutation.

    Args:
        action: Dispatcher action that produced the result.
        result: Complete dispatcher result.

    Returns:
        Original result unless project routing changed; otherwise a copy that
        reports the completed MCP-local refresh.
    """
    if action != "project" or not isinstance(result, dict):
        return result
    if result.get("reinitialize_required") is not True:
        return result

    global _CW, _ROOT

    refresh_started_ns = time.perf_counter_ns() if telemetry is not None else None
    try:
        _initialize_dispatcher(reset=False)
    except Exception as exc:
        if telemetry is not None:
            assert refresh_started_ns is not None
            telemetry.routing_status = "failed"
            telemetry.routing_elapsed_ms = telemetry.elapsed_ms(refresh_started_ns)
        _CW = None
        _ROOT = None
        _table_cache.clear()
        raise RuntimeError(
            "Project change persisted, but MCP routing refresh failed. "
            "The next request will retry initialization."
        ) from exc
    if telemetry is not None:
        assert refresh_started_ns is not None
        telemetry.routing_status = "succeeded"
        telemetry.routing_elapsed_ms = telemetry.elapsed_ms(refresh_started_ns)
    refreshed = dict(result)
    refreshed["reinitialize_required"] = False
    refreshed["routing_refreshed"] = True
    refreshed["routing_stale"] = False
    refreshed["task_state_reset"] = True
    refreshed["orientation_required"] = True
    refreshed["fresh_task_required"] = True
    refreshed["suggested_next_actions"] = [
        "Task authorization was reset. Run anchor('orient'), then start a fresh task in the selected project."
    ]
    refreshed.pop("operating_protocol", None)
    refreshed.pop("agent_context", None)
    # Read a projection directly from the refreshed runtime. Never invoke a
    # lifecycle action merely to decorate a transport response.
    snapshot = getattr(_CW, "_operating_protocol_snapshot", None)
    if callable(snapshot):
        try:
            protocol = snapshot()
        except Exception:
            protocol = None
        if isinstance(protocol, dict):
            refreshed["operating_protocol"] = protocol
    context_snapshot = getattr(_CW, "_agent_context_snapshot", None)
    if callable(context_snapshot):
        try:
            agent_context = context_snapshot()
        except Exception:
            agent_context = None
        if isinstance(agent_context, dict):
            refreshed["agent_context"] = agent_context
    return refreshed


def _compact_task_result(result: dict[str, Any]) -> dict[str, Any]:
    """Project a full task result to the decision-critical MCP response contract.

    Args:
        result: Complete task execution context returned by the dispatcher.

    Returns:
        Compact transport projection with explicit lossless retrieval guidance.
    """
    compact = {key: result[key] for key in _COMPACT_TASK_KEYS if key in result}
    compact["transport"] = {
        "response_detail": "compact",
        "full_response_available": True,
        "omitted_sections": sorted(set(result) - set(compact)),
    }
    return compact


def _prepare_response(
    action: str,
    result: Any,
    response_detail: str,
    telemetry: _McpTelemetry | None = None,
) -> Any:
    """Apply MCP-only routing and output behavior to a core dispatcher result.

    Args:
        action: Dispatcher action that produced the result.
        result: Complete core dispatcher result.
        response_detail: Requested transport detail, `compact` or `full`.

    Returns:
        Transport-ready result without changing the core action contract.
    """
    prepared = _refresh_after_project_change(action, result, telemetry)
    if action == "task" and response_detail == "compact" and isinstance(prepared, dict):
        return _compact_task_result(prepared)
    return prepared





# ─── Consolidated Gateway Tools ──────────────────────────────────────────────
# Single universal gateway for every registered anchor() action — uses only 2 MCP tool slots


@mcp.tool()
def anchor_execute(
    action: str,
    args: str | None = None,
    response_version: _ResponseVersion = 1,
    response_detail: _ResponseDetail = "compact",
    telemetry_version: _TelemetryVersion | None = None,
) -> str:
    """Execute any odibi_anchor anchor() action.

    This is a universal gateway to all registered anchor() actions. Call anchor_help() first
    to see available actions organized by category.

    Args:
        action: Action name (e.g. 'profile_table', 'quality', 'task', 'gate', 'memory')
        args: JSON string of keyword arguments.
              Example: '{"table": "catalog.schema.table", "level": "quick"}'
              For actions with positional args, use special keys like "arg0", "arg1".
        response_version: Response envelope version, 1 or 2.
        response_detail: `compact` returns decision-critical task fields; `full`
            returns the complete task result. Non-task actions are unchanged.
        telemetry_version: Explicit telemetry schema version. Version 1 requires
            response_version 2.

    Examples:
        # Data profiling
        anchor_execute("profile_table", '{"table": "main.default.orders", "level": "quick"}')

        # Planning task
        anchor_execute("task", '{"arg0": "Fix null handling", "goal": "...", "mode": "implementation"}')

        # Quality gate
        anchor_execute("quality", '{"table": "staging.clean.users", "keys": ["user_id"]}')

        # Capture and assess evidence-backed reusable learning
        anchor_execute("learning", '{"arg0":"capture","observation_type":"reusable_practice","summary":"...","signal_key":"example.signal","evidence":[...]}')

        # Session status
        anchor_execute("status", None)

        # Get help
        anchor_execute("help", '{"arg0": "profile_table"}')

    Returns:
        Compact JSON string with anchor() action result (findings, risks, metrics, suggestions).
    """
    response_version = _validate_response_version(response_version)
    response_detail = _validate_response_detail(response_detail)
    telemetry_version = _validate_telemetry_version(telemetry_version)
    if telemetry_version == 1 and response_version != 2:
        raise ValueError("telemetry_version=1 requires response_version=2")
    if telemetry_version == 1:
        return _cw_execute_with_telemetry(action, args, response_detail)
    with _GATEWAY_LOCK:
        try:
            try:
                anchor = _boot()
                payload: dict[str, Any] = {"action": action}
                if args is not None:
                    parsed = decode_json_document(args)
                    explicit_kwargs = parsed.pop("kwargs", {})
                    if not isinstance(explicit_kwargs, dict):
                        raise ValueError("kwargs must be a JSON object")
                    keyword_args = dict(explicit_kwargs)
                    for key in list(parsed):
                        if key in {"schema_version", "args"} or (
                            key.startswith("arg") and key[3:].isdigit()
                        ):
                            payload[key] = parsed.pop(key)
                    for key, value in parsed.items():
                        if key in keyword_args:
                            raise ValueError(f"duplicate keyword representation: {key}")
                        keyword_args[key] = value
                    payload["kwargs"] = keyword_args
                request = normalize_request(payload, resolver=_resolve_request_value)
            except Exception as exc:
                if response_version == 2:
                    return _fmt({"ok": False, "error": error_information(exc)})
                raise
            if response_version == 2:
                envelope = execute_request(anchor, request)
                if envelope["ok"]:
                    try:
                        envelope["result"] = _prepare_response(
                            request.action, envelope["result"], response_detail,
                        )
                    except Exception as exc:
                        envelope = {"ok": False, "error": error_information(exc)}
                return _fmt(envelope)
            result = anchor(request.action, *request.args, **request.kwargs)
            return _fmt(_prepare_response(request.action, result, response_detail))
        finally:
            _close_memory_connections()


def _cw_execute_with_telemetry(action: str, args: str | None, response_detail: str) -> str:
    """Execute the explicit v2 telemetry path without changing the legacy path."""
    started_ns = time.perf_counter_ns()
    telemetry = _McpTelemetry(started_ns)
    lock_started_ns = time.perf_counter_ns()
    with _GATEWAY_LOCK:
        telemetry.stages_ms["gateway_lock_wait"] = telemetry.elapsed_ms(lock_started_ns)
        base_text: str
        try:
            try:
                if _CW is None:
                    with telemetry.stage("bootstrap"):
                        anchor = _boot()
                else:
                    anchor = _CW
                with telemetry.stage("normalization_resolution"):
                    payload: dict[str, Any] = {"action": action}
                    if args is not None:
                        parsed = decode_json_document(args)
                        explicit_kwargs = parsed.pop("kwargs", {})
                        if not isinstance(explicit_kwargs, dict):
                            raise ValueError("kwargs must be a JSON object")
                        keyword_args = dict(explicit_kwargs)
                        for key in list(parsed):
                            if key in {"schema_version", "args"} or (
                                key.startswith("arg") and key[3:].isdigit()
                            ):
                                payload[key] = parsed.pop(key)
                        for key, value in parsed.items():
                            if key in keyword_args:
                                raise ValueError(f"duplicate keyword representation: {key}")
                            keyword_args[key] = value
                        payload["kwargs"] = keyword_args
                    request = normalize_request(payload, resolver=_resolve_request_value)
            except Exception as exc:
                envelope = {"ok": False, "error": error_information(exc)}
            else:
                with telemetry.stage("dispatch"):
                    envelope = execute_request(anchor, request)
                if envelope["ok"]:
                    try:
                        with telemetry.stage("response_preparation"):
                            envelope["result"] = _prepare_response(
                                request.action, envelope["result"], response_detail, telemetry,
                            )
                    except Exception as exc:
                        envelope = {"ok": False, "error": error_information(exc)}
            with telemetry.stage("serialization"):
                base_text = _fmt(envelope)
        finally:
            with telemetry.stage("connection_cleanup"):
                _close_memory_connections()

        total_ms = telemetry.elapsed_ms(started_ns)
        enriched = dict(envelope)
        enriched["telemetry"] = {
            "schema_version": 1,
            "clock": "monotonic",
            "scope": "mcp_server",
            "server_total_ms": total_ms,
            "stages_ms": telemetry.stages_ms,
            "routing_reinitialization": {
                "status": telemetry.routing_status,
                "elapsed_ms": telemetry.routing_elapsed_ms,
            },
            "response": {
                "basis": "v2_envelope_without_telemetry",
                "utf8_bytes": len(base_text.encode("utf-8")),
                "estimated_tokens": estimate_tokens(base_text),
            },
            "excludes": [
                "telemetry_encoding", "fastmcp_transport", "network", "client_harness",
            ],
        }
        return _fmt(enriched)


@mcp.tool()
def anchor_help(category: str | None = None, action: str | None = None) -> str:
    """Get comprehensive help for all anchor() actions.

    This tool provides discovery for all anchor() actions available through anchor_execute().
    Use it to find the right action for your task and learn its parameters.

    Args:
        category: Filter by category (optional). Available categories:
                  - "Codebase & Memory" — memory, map, impact, safe, semantic
                  - "Planning & Workflow" — task, gate, preflight, test, checkpoint, spec, problem, work_item
                  - "Data & Profiling" — profile_table, microscope, quality, validate, transform
                  - "Debugging & Learning" — known_error, trace, lookup, learn, save
                  - "Session & Snapshots" — snapshot, save_snap, load_snap, archive
                  - "Diagnostics" — memory_stats, session_files, status, config, project, frame
                  - "File Tracking" — touched, log, session_log
                  - "Session Notebooks" — new_session
                  - "Testing" — dogfood
                  - "Composed Workflows" — reconcile, investigate, debug, trace_row, evolve, chain
        action: Get detailed help for a specific action (optional).
                Example: "profile_table", "quality", "task"

    Returns:
        - If action specified: Detailed signature and usage for that action
        - If category specified: All actions in that category with signatures
        - If neither specified: Complete action catalog organized by category

    Examples:
        # Get full action catalog
        anchor_help()

        # Get all data profiling actions
        anchor_help(category="Data & Profiling")

        # Get detailed help for specific action
        anchor_help(action="profile_table")
    """
    with _GATEWAY_LOCK:
        try:
            anchor = _boot()

            # If specific action requested, use anchor("help", action)
            if action:
                return _fmt(anchor("help", action))

            # Otherwise return ACTION_GROUPS catalog
            if category:
                # Filter by category
                actions = ACTION_GROUPS.get(category, [])
                if not actions:
                    available = ", ".join(ACTION_GROUPS.keys())
                    return _fmt({
                        "error": f"Unknown category: {category}",
                        "available_categories": list(ACTION_GROUPS.keys()),
                        "tip": f"Available categories: {available}"
                    })
                result = {
                    "category": category,
                    "action_count": len(actions),
                    "actions": {a: DISPATCH_SIGS.get(a, "No signature available") for a in actions}
                }
            else:
                # Return full catalog
                result = {
                    "summary": f"odibi_anchor has {sum(len(acts) for acts in ACTION_GROUPS.values())} anchor() actions across 10 categories",
                    "usage": "Call anchor_execute(action, args) to run any action. Use anchor_help(category=...) to filter.",
                    "categories": {
                        cat: {
                            "count": len(actions),
                            "actions": {a: DISPATCH_SIGS.get(a, "") for a in actions}
                        }
                        for cat, actions in ACTION_GROUPS.items()
                    },
                    "total_actions": sum(len(actions) for actions in ACTION_GROUPS.values())
                }

            return _fmt(result)
        finally:
            _close_memory_connections()


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Boot the anchor dispatcher EAGERLY, before the stdio transport starts, and warm
    # the heavy lazy imports the data tools pull in (pandas + the table_profiler
    # tool modules). Doing these the first time INSIDE a tool handler — while
    # FastMCP is mid-request over stdio — swallowed that first response (the call
    # hung; every later call worked). Warming them here keeps every tool call,
    # including the first data call, a clean fast dispatch. All under
    # redirect_stdout so any import chatter never touches the protocol stream.
    with contextlib.redirect_stdout(sys.stderr):
        _boot()
        try:
            import pandas  # noqa: F401

            import tools.table_profiler_tool.lib.case_file
            import tools.table_profiler_tool.lib.microscope
            import tools.table_profiler_tool.lib.profiler  # noqa: F401
        except Exception:
            pass
    mcp.run()
