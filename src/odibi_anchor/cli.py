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
    execute_request,
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
    commands.add_parser("doctor", help="inspect startup and routing without mutation")
    guidance = commands.add_parser("install-guidance", help="copy packaged agent guidance into a repository")
    guidance.add_argument("target", help="existing repository root; existing guidance is never overwritten")
    verify = commands.add_parser("verify-delivery", help="verify local delivery evidence under declared policy")
    verify.add_argument("--request", required=True, help="UTF-8 JSON request file, or - for stdin")
    return parser


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
    with contextlib.redirect_stdout(sys.stderr):
        response = execute_request(dispatcher, normalized)
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
        try:
            from odibi_anchor.startup import doctor

            return _emit({"ok": True, "result": doctor()})
        except Exception as exc:
            return _emit({"ok": False, "error": error_information(exc)}, EXIT_BOOTSTRAP)
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
