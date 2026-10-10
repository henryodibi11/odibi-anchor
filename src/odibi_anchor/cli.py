"""Standard-library command line interface for odibi-anchor."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import replace
from typing import Any

from odibi_anchor._dispatcher._request_adapter import (
    SCHEMA_VERSION,
    BootstrapError,
    NormalizedRequest,
    RequestError,
    decode_json_document,
    error_information,
    normalize_request,
)

EXIT_INPUT = 2
EXIT_POLICY = 3
EXIT_ACTION = 4
EXIT_BOOTSTRAP = 5
_MAX_JSON_BYTES = 1_048_576


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _emit(value: Any, code: int = 0, *, flush: bool = False) -> int:
    """Emit strict JSON, replacing untransportable results with an action error."""
    try:
        payload = _json(value)
        if len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError("output exceeds transport bound")
    except Exception:
        payload = _json({
            "ok": False,
            "error": {
                "type": "SerializationError",
                "message": "action result is not strict JSON-serializable",
            },
        })
        code = max(code, EXIT_ACTION)
    print(payload, flush=flush)
    return code


def _configure_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


class _StderrParser(argparse.ArgumentParser):
    def print_help(self, file=None):
        super().print_help(file=sys.stderr if file is None else file)

    def error(self, message):
        raise RequestError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _StderrParser(prog="anchor", description="Execute odibi-anchor requests as deterministic JSON.")
    parser.add_argument("--root", help="target project root (defaults to normal bootstrap selection)")
    parser.add_argument("--format", choices=("json",), default="json", help="JSON transport output (core dict format)")
    commands = parser.add_subparsers(dest="command", required=True)
    exec_state_note = "execute once; separate processes do not share session state"
    execute = commands.add_parser("exec", help=exec_state_note, description=exec_state_note)
    execute.add_argument("action")
    execute.add_argument("request", nargs="?", default="{}", help="JSON object containing args/kwargs or arg0..argN")
    batch = commands.add_parser("batch", help="boot once and execute a JSON array or JSON-lines stream")
    batch.add_argument("file", nargs="?", default="-", help="UTF-8 input file, or - for stdin")
    commands.add_parser("shell", help="boot once; read one request JSON object per input line")
    commands.add_parser("help", help="discover core actions")
    doctor = commands.add_parser("doctor", help="inspect startup and routing without mutation")
    doctor.add_argument(
        "--fresh-compute", action="store_true",
        help="plan one project's launch on a fresh compute identity, read-only",
    )
    doctor.add_argument("--config", help="absolute portfolio path (with --fresh-compute)")
    doctor.add_argument("--host", help="portfolio host ID (with --fresh-compute)")
    doctor.add_argument("--project", help="managed project ID (with --fresh-compute)")
    setup = commands.add_parser("setup-host", help="idempotently install managed host guidance")
    setup.add_argument("adapter", choices=("amp", "chatgpt", "claude", "databricks"))
    setup.add_argument("--target", required=True, help="existing explicit host instruction root")
    setup.add_argument(
        "--portfolio", help="absolute portfolio path to record in the host binding sidecar"
    )
    setup.add_argument("--host", help="portfolio host ID for --portfolio (default: unique match)")
    setup.add_argument(
        "--reconcile", action="store_true",
        help="classify drift and plan backup plus reinstall (dry run unless --apply)",
    )
    setup.add_argument("--apply", action="store_true", help="apply the --reconcile plan")
    setup.add_argument(
        "--approve-replace-edited", action="store_true",
        help="with --reconcile --apply, back up and replace unmanaged edits",
    )
    guidance = commands.add_parser("install-guidance", help="copy packaged agent guidance into a repository")
    guidance.add_argument("target", help="existing repository root; existing guidance is never overwritten")
    verify = commands.add_parser("verify-delivery", help="verify local delivery evidence under declared policy")
    verify.add_argument("--request", required=True, help="UTF-8 JSON request file, or - for stdin")
    portfolio = commands.add_parser("portfolio", help="manage one explicit PortfolioV1 configuration")
    portfolio_commands = portfolio.add_subparsers(dest="portfolio_command", required=True)
    portfolio_commands.add_parser("schema", help="show the PortfolioV1 schema")
    show = portfolio_commands.add_parser("show", help="load a portfolio and show its digest")
    show.add_argument("--config", required=True)
    validate = portfolio_commands.add_parser("validate", help="validate portfolio launch readiness")
    validate.add_argument("--config", required=True)
    validate.add_argument("--host")
    scaffold = portfolio_commands.add_parser("scaffold", help="create a safe incomplete portfolio")
    scaffold.add_argument("--config", required=True)
    scaffold.add_argument("--host", required=True)
    scaffold.add_argument("--adapter", required=True, choices=("amp", "chatgpt", "claude", "databricks"))
    scaffold.add_argument("--target-root", required=True)
    scaffold.add_argument("--project")
    scaffold.add_argument("--authority")
    scaffold.add_argument("--local-state-root")
    scaffold.add_argument("--instruction-root")
    scaffold.add_argument("--durable-root")
    add = portfolio_commands.add_parser("add-project", help="add one exact host/project target")
    add.add_argument("--config", required=True)
    add.add_argument("--host", required=True)
    add.add_argument("--project", required=True)
    add.add_argument("--target-root", required=True)
    add.add_argument("--repository")
    add.add_argument("--artifact-namespace")
    add.add_argument("--expected-sha256")
    resolve = portfolio_commands.add_parser("resolve", help="resolve immutable route inputs")
    resolve.add_argument("--config", required=True)
    resolve.add_argument("--host", required=True)
    resolve.add_argument("--project")
    resolve.add_argument("--target-root")
    resolve.add_argument("--persona")
    prepare = portfolio_commands.add_parser("prepare", help="register and restore one exact runtime")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--host", required=True)
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--persona")
    repair = portfolio_commands.add_parser(
        "repair-descriptor",
        help="owner-approved repair of one damaged PROJECT.md route frontmatter (dry run without --approve)",
    )
    repair.add_argument("--config", required=True)
    repair.add_argument("--host", required=True)
    repair.add_argument("--project", required=True)
    repair.add_argument("--expected-sha256", required=True)
    repair.add_argument("--approve", action="store_true")
    move = portfolio_commands.add_parser(
        "move-target", help="journaled, hash-verified move of one or more project targets",
    )
    move.add_argument("--config", required=True)
    move.add_argument("--host", required=True)
    move.add_argument("--project")
    move.add_argument("--from", dest="from_target")
    move.add_argument("--to", dest="to_target")
    move.add_argument("--mapping", help='batch JSON file {"moves": [{"project", "from", "to"}]}')
    move_mode = move.add_mutually_exclusive_group()
    move_mode.add_argument("--dry-run", action="store_true")
    move_mode.add_argument("--resume", action="store_true")
    move_mode.add_argument("--rollback", action="store_true")
    state = commands.add_parser("state", help="inspect or transfer durable Anchor state")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    for name in ("list", "snapshot", "restore", "resume", "abandon"):
        command = state_commands.add_parser(name, help={
            "resume": "finish an incomplete restore against its recorded snapshot",
            "abandon": "move an incomplete restore to a preserved quarantine path",
        }.get(name))
        command.add_argument("--durable-root", required=True)
        command.add_argument("--authority", required=True)
        command.add_argument("--databricks", action="store_true")
        if name != "list":
            command.add_argument("--database", required=True)
            command.add_argument(
                "--artifacts",
                required=name in {"resume", "abandon"},
                help="absolute managed projects directory for v2 snapshot or restore",
            )
    return parser


def _portfolio_command(ns: argparse.Namespace) -> dict[str, Any]:
    from odibi_anchor.portfolio import (
        add_project,
        load_portfolio_document,
        portfolio_schema,
        resolve_project,
        scaffold_portfolio,
        validate_portfolio,
    )

    if ns.portfolio_command == "schema":
        return {"schema": portfolio_schema(), "next_operation": {"operation": "portfolio.scaffold"}}
    if ns.portfolio_command == "repair-descriptor":
        from odibi_anchor.startup import repair_portfolio_descriptor

        return repair_portfolio_descriptor(
            config_path=ns.config, host_id=ns.host, project_id=ns.project,
            expected_sha256=ns.expected_sha256, approve=ns.approve,
        )
    if ns.portfolio_command == "scaffold":
        return scaffold_portfolio(
            ns.config, host_id=ns.host, adapter=ns.adapter, target_root=ns.target_root,
            project_id=ns.project, authority_id=ns.authority,
            local_state_root=ns.local_state_root, instruction_root=ns.instruction_root,
            durable_root=ns.durable_root,
        )
    if ns.portfolio_command == "move-target":
        from odibi_anchor._migration import load_mapping, move_target, move_targets

        single = (ns.project, ns.from_target, ns.to_target)
        if ns.mapping is not None:
            if any(value is not None for value in single) or ns.resume or ns.rollback:
                raise RequestError("--mapping excludes --project/--from/--to, --resume and --rollback")
            try:
                moves = load_mapping(ns.mapping)
            except (OSError, UnicodeError, ValueError) as exc:
                raise RequestError(f"invalid --mapping file: {exc}") from exc
            return move_targets(
                config_path=ns.config, host_id=ns.host, moves=moves, dry_run=ns.dry_run,
            )
        if any(value is None for value in single):
            raise RequestError("move-target requires --project, --from and --to (or --mapping)")
        return move_target(
            config_path=ns.config, host_id=ns.host, project_id=ns.project,
            from_target=ns.from_target, to_target=ns.to_target,
            dry_run=ns.dry_run, resume=ns.resume, rollback=ns.rollback,
        )
    document = load_portfolio_document(ns.config)
    if ns.portfolio_command == "show":
        return document
    if ns.portfolio_command == "validate":
        return {**document, "validation": validate_portfolio(document["portfolio"], host_id=ns.host)}
    if ns.portfolio_command == "add-project":
        return add_project(
            ns.config, project_id=ns.project, host_id=ns.host, target_root=ns.target_root,
            repository=ns.repository, artifact_namespace=ns.artifact_namespace,
            expected_sha256=ns.expected_sha256,
        )
    if ns.portfolio_command == "resolve":
        return {
            **resolve_project(
                document["portfolio"], host_id=ns.host, project_id=ns.project,
                target_root=ns.target_root, persona_id=ns.persona,
            ),
            "config_path": document["path"],
            "config_sha256": document["sha256"],
        }
    if ns.portfolio_command == "prepare":
        from odibi_anchor.startup import prepare_portfolio_runtime

        return prepare_portfolio_runtime(
            config_path=ns.config, host_id=ns.host, project_id=ns.project,
            persona_id=ns.persona,
        )
    raise RequestError("unsupported portfolio command")


def _state_command(ns: argparse.Namespace) -> dict[str, Any]:
    from odibi_anchor.durability import (
        abandon_restore,
        list_snapshots,
        restore_latest,
        resume_restore,
        snapshot_state,
    )

    if ns.state_command == "list":
        return list_snapshots(
            durable_root=ns.durable_root,
            authority_id=ns.authority,
            databricks=ns.databricks,
        )
    if ns.state_command == "snapshot":
        return snapshot_state(
            source_db=ns.database,
            source_artifacts=ns.artifacts,
            durable_root=ns.durable_root,
            authority_id=ns.authority,
            databricks=ns.databricks,
        )
    if ns.state_command == "restore":
        return restore_latest(
            durable_root=ns.durable_root,
            destination_db=ns.database,
            destination_artifacts=ns.artifacts,
            authority_id=ns.authority,
            databricks=ns.databricks,
        )
    if ns.state_command in {"resume", "abandon"}:
        operation = resume_restore if ns.state_command == "resume" else abandon_restore
        return operation(
            durable_root=ns.durable_root,
            destination_db=ns.database,
            destination_artifacts=ns.artifacts,
            authority_id=ns.authority,
            databricks=ns.databricks,
        )
    raise RequestError("unsupported state command")


_MUTATING_OPERATIONS = frozenset({
    "portfolio.scaffold", "portfolio.add-project", "portfolio.prepare",
    "state.snapshot", "state.restore", "state.resume", "state.abandon",
})
_OPERATION_UNDO = {
    "state.snapshot": "snapshot publications are immutable; the live database is unchanged",
    "state.restore": "Anchor never deletes restored state; the durable snapshot is unchanged",
    "state.resume": "the finalized restore is not rolled back; the durable snapshot is unchanged",
    "state.abandon": "the abandoned tree is preserved at quarantine_path; restore again with next_operation",
    "portfolio.scaffold": "no supported portfolio removal command exists; ask the owner",
    "portfolio.add-project": "no supported project removal command exists; ask the owner",
    "portfolio.prepare": "prepared runtime registration is not withdrawn by a command",
}


def _readback(operation: str, ns: argparse.Namespace, result: dict[str, Any]) -> dict[str, Any] | None:
    """Re-read the exact state an operation wrote; never infer success from the result alone."""
    import hashlib
    import os

    if operation in {"portfolio.scaffold", "portfolio.add-project"} and isinstance(result.get("sha256"), str):
        from odibi_anchor.portfolio import load_portfolio_document

        document = load_portfolio_document(ns.config)
        observed = document["sha256"] == result["sha256"]
        if operation == "portfolio.add-project":
            observed = observed and result.get("project_id") in document["portfolio"].get("projects", {})
        return {"method": "portfolio_document_sha256", "observed": observed}
    if operation == "state.restore" and isinstance(result.get("restored_sha256"), str):
        with open(result["destination_db"], "rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        return {"method": "destination_db_sha256", "observed": digest == result["restored_sha256"]}
    if operation == "state.resume" and result.get("destination_db"):
        return {"method": "destination_db_exists", "observed": os.path.isfile(result["destination_db"])}
    if operation == "state.abandon" and result.get("quarantine_path"):
        return {
            "method": "quarantine_preserved",
            "observed": os.path.isdir(result["quarantine_path"])
            and not os.path.lexists(result["destination_artifacts"]),
        }
    return None


def _enveloped(operation: str, ns: argparse.Namespace, result: Any) -> Any:
    """Attach the standard result envelope to a non-dispatcher CLI operation result."""
    from odibi_anchor._dispatcher._envelope import ENVELOPE_KEY, build_operation_envelope

    if not isinstance(result, dict):
        return result
    mutating = operation in _MUTATING_OPERATIONS
    try:
        readback = _readback(operation, ns, result) if mutating else None
    except (OSError, ValueError) as exc:
        readback = {"method": "readback_failed", "observed": False, "error": type(exc).__name__}
    undo = (
        {"status": "irreversible", "copy_ready": None, "reason": _OPERATION_UNDO[operation]}
        if operation in _OPERATION_UNDO else None
    )
    result[ENVELOPE_KEY] = build_operation_envelope(
        operation, result, mutating=mutating, readback=readback, undo=undo,
    )
    return result


def _operation_error(operation: str, exc: Exception) -> dict[str, Any]:
    from odibi_anchor._dispatcher._envelope import build_operation_failure_envelope

    return {
        **error_information(exc),
        "envelope": build_operation_failure_envelope(
            operation, exc, mutating=operation in _MUTATING_OPERATIONS,
        ),
    }


def _boot(root: str | None):
    # Bootstrap is historically chatty. Keep the CLI's machine stream pristine.
    from odibi_anchor.bootstrap import init

    with contextlib.redirect_stdout(sys.stderr):
        try:
            dispatcher, _, _ = init(root=root, output_format="dict")
        except Exception as exc:
            raise BootstrapError(str(exc)) from exc
    return dispatcher


def _request(raw: Any, dispatcher: Any, *, shared: bool) -> tuple[dict[str, Any], int]:
    try:
        normalized = raw if isinstance(raw, NormalizedRequest) else normalize_request(raw)
        # ``json`` names the transport encoding, not a core renderer.  The
        # dispatcher has always called its structured representation ``dict``.
        normalized = replace(normalized, kwargs={**normalized.kwargs, "output_format": "dict"})
    except RequestError as exc:
        return {"ok": False, "error": {"type": "input", "message": error_information(exc)["message"]}}, EXIT_INPUT
    from odibi_anchor._dispatcher._envelope import execute_with_envelope

    with contextlib.redirect_stdout(sys.stderr):
        response = execute_with_envelope(dispatcher, normalized)
    response["metadata"] = {"schema_version": SCHEMA_VERSION, "process_state_shared": shared}
    if response["ok"]:
        return response, 0
    message = response["error"]["message"]
    if response["error"]["type"] == "RuntimeError" and message.strip().startswith("BLOCKED:"):
        return response, EXIT_POLICY
    if message.startswith("Unknown anchor() action:"):
        return response, EXIT_ACTION
    return response, EXIT_ACTION


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_bounded_utf8(path: str, limit: int) -> str:
    """Read at most one bounded UTF-8 document without normalizing file bytes."""
    if path == "-":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        value = stream.read(limit + 1)
    else:
        with open(path, "rb") as handle:
            value = handle.read(limit + 1)
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > limit:
            raise ValueError(f"request exceeds {limit} bytes")
        return value
    if len(value) > limit:
        raise ValueError(f"request exceeds {limit} bytes")
    return value.decode("utf-8", "strict")


def _prepare(ns: argparse.Namespace) -> list[NormalizedRequest]:
    """Read and strictly validate all finite CLI input before bootstrap."""
    if ns.command == "exec":
        body = decode_json_document(ns.request)
        if "action" in body:
            raise RequestError("duplicate action representations: command argument and request body")
        body["action"] = ns.action
        return [normalize_request(body)]
    if ns.command == "help":
        return [normalize_request({"action": "help"})]
    if ns.command == "shell":
        return []
    text = _read(ns.file)
    if text.lstrip().startswith("["):
        values = decode_json_document(text, array=True)
        return [normalize_request(value) for value in values]
    return [normalize_request(line) for line in text.splitlines() if line.strip()]


def _run_shell(dispatcher: Any) -> int:
    """Execute strict JSON-line requests incrementally in one stateful process."""
    worst = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        result, code = _request(line, dispatcher, shared=True)
        worst = max(worst, _emit(result, code, flush=True))
    return worst


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its stable process exit code."""
    _configure_utf8()
    try:
        ns = _parser().parse_args(argv)
    except RequestError as exc:
        _emit({"ok": False, "error": {"type": "input", "message": error_information(exc)["message"]}})
        return EXIT_INPUT
    if ns.command == "verify-delivery":
        try:
            from odibi_anchor.delivery import DeliveryInputError, verify_delivery
            from odibi_anchor.operational._contract import MAX_PAYLOAD_BYTES
            try:
                text = _read_bounded_utf8(ns.request, MAX_PAYLOAD_BYTES)
            except ValueError as exc:
                raise DeliveryInputError(str(exc)) from exc
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise DeliveryInputError(f"duplicate JSON key: {key}")
                    result[key] = value
                return result
            try:
                request = json.loads(text, object_pairs_hook=pairs,
                                     parse_constant=lambda value: (_ for _ in ()).throw(
                                         DeliveryInputError(f"non-finite JSON number: {value}")))
            except (RecursionError, ValueError) as exc:
                if isinstance(exc, DeliveryInputError):
                    raise
                raise DeliveryInputError(f"invalid JSON document: {type(exc).__name__}") from exc
            result = verify_delivery(request).to_dict()
            return _emit(result, 0 if result["eligible_under_declared_policy"] else EXIT_POLICY)
        except (OSError, UnicodeError, json.JSONDecodeError, DeliveryInputError) as exc:
            return _emit({"ok": False, "error": {"type": "input", "message": str(exc)}}, EXIT_INPUT)
        except Exception as exc:
            return _emit({"ok": False, "error": {"type": type(exc).__name__,
                                                   "message": "delivery verification failed"}}, EXIT_ACTION)
    if ns.command == "doctor":
        fresh_inputs = (ns.config, ns.host, ns.project)
        if (ns.fresh_compute and None in fresh_inputs) or (
            not ns.fresh_compute and any(value is not None for value in fresh_inputs)
        ):
            return _emit({"ok": False, "error": {
                "type": "input",
                "message": "--fresh-compute requires --config, --host and --project, "
                           "and those options require --fresh-compute",
            }}, EXIT_INPUT)
        try:
            from odibi_anchor.startup import doctor

            if ns.fresh_compute:
                return _emit({"ok": True, "result": doctor(
                    fresh_compute=True, config_path=ns.config, host_id=ns.host,
                    project_id=ns.project,
                )})
            return _emit({"ok": True, "result": doctor()})
        except Exception as exc:
            return _emit({"ok": False, "error": error_information(exc)}, EXIT_BOOTSTRAP)
    if ns.command == "setup-host":
        if (ns.apply or ns.approve_replace_edited) and not ns.reconcile:
            return _emit({"ok": False, "error": {
                "type": "input",
                "message": "--apply and --approve-replace-edited require --reconcile",
            }}, EXIT_INPUT)
        if ns.host is not None and ns.portfolio is None:
            return _emit({"ok": False, "error": {
                "type": "input", "message": "--host requires --portfolio",
            }}, EXIT_INPUT)
        try:
            from odibi_anchor.host_setup import setup_host

            options: dict[str, Any] = {}
            if ns.portfolio is not None:
                options.update(portfolio_config=ns.portfolio, host_id=ns.host)
            if ns.reconcile:
                options.update(
                    reconcile=True, dry_run=not ns.apply,
                    approve_replace_edited=ns.approve_replace_edited,
                )
            return _emit({"ok": True, "result": setup_host(
                ns.target, adapter=ns.adapter, **options
            )})
        except (OSError, RuntimeError, ValueError) as exc:
            return _emit({"ok": False, "error": error_information(exc)}, EXIT_ACTION)
    if ns.command == "portfolio":
        operation = f"portfolio.{ns.portfolio_command}"
        try:
            return _emit({"ok": True, "result": _enveloped(operation, ns, _portfolio_command(ns))})
        except RequestError as exc:
            return _emit({"ok": False, "error": {"type": "input", "message": str(exc)}}, EXIT_INPUT)
        except (OSError, RuntimeError, ValueError) as exc:
            return _emit({"ok": False, "error": _operation_error(operation, exc)}, EXIT_ACTION)
    if ns.command == "state":
        operation = f"state.{ns.state_command}"
        if ns.state_command == "list":
            try:
                return _emit({"ok": True, "result": _state_command(ns)})
            except (OSError, RuntimeError, ValueError) as exc:
                return _emit({"ok": False, "error": error_information(exc)}, EXIT_ACTION)
        try:
            return _emit({"ok": True, "result": _enveloped(operation, ns, _state_command(ns))})
        except (OSError, RuntimeError, ValueError) as exc:
            return _emit({"ok": False, "error": _operation_error(operation, exc)}, EXIT_ACTION)
    if ns.command == "install-guidance":
        try:
            from odibi_anchor.startup import install_guidance

            return _emit({"ok": True, "result": install_guidance(ns.target)})
        except (OSError, RuntimeError, ValueError) as exc:
            return _emit({"ok": False, "error": error_information(exc)}, EXIT_ACTION)
    try:
        prepared = _prepare(ns)
    except (OSError, UnicodeError, RequestError) as exc:
        _emit({"ok": False, "error": {"type": "input", "message": error_information(exc)["message"]}})
        return EXIT_INPUT
    try:
        dispatcher = _boot(ns.root)
    except Exception as exc:
        _emit({"ok": False, "error": {"category": "bootstrap", **error_information(exc)}})
        return EXIT_BOOTSTRAP
    if ns.command == "exec":
        print("warning: separate 'anchor exec' processes do not share session state", file=sys.stderr)
        result, code = _request(prepared[0], dispatcher, shared=False)
        return _emit(result, code)
    if ns.command == "help":
        result, code = _request(prepared[0], dispatcher, shared=False)
        return _emit(result, code)
    if ns.command == "shell":
        return _run_shell(dispatcher)
    try:
        worst = 0
        for raw in prepared:
            result, code = _request(raw, dispatcher, shared=True)
            worst = max(worst, _emit(result, code, flush=True))
        return worst
    except (OSError, UnicodeError, RequestError) as exc:
        _emit({"ok": False, "error": {"type": "input", "message": error_information(exc)["message"]}})
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
