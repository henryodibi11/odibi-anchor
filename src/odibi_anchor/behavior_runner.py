"""Isolated executor for source-owned, allowlisted Python behavior claims."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

FORMAT = "odibi-anchor-python-call-result-v1"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _resolve_pointer(value: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("assertion path must be a JSON pointer")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise ValueError("assertion path is unavailable in callable result")
    return current


def execute(request: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Execute one already-authorized request and return only bounded comparison evidence."""
    if set(request) != {"module", "qualname", "args", "kwargs", "assertions"}:
        raise ValueError("behavior request has unexpected fields")
    module_name = request["module"]
    qualname = request["qualname"]
    args = request["args"]
    kwargs = request["kwargs"]
    assertions = request["assertions"]
    if (
        not isinstance(module_name, str)
        or not all(_NAME.fullmatch(part) for part in module_name.split("."))
        or not isinstance(qualname, str)
        or not all(_NAME.fullmatch(part) for part in qualname.split("."))
        or not isinstance(args, list)
        or not isinstance(kwargs, dict)
        or not isinstance(assertions, list)
    ):
        raise ValueError("behavior request is malformed")

    for candidate in (root / "src", root):
        resolved = str(candidate.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise ValueError("allowlisted behavior target is not callable")
    returned = target(*args, **kwargs)

    comparisons = []
    for assertion in assertions:
        if not isinstance(assertion, dict) or set(assertion) != {"path", "equals"}:
            raise ValueError("behavior assertion is malformed")
        observed = _resolve_pointer(returned, assertion["path"])
        observed_json = _canonical(observed)
        expected_json = _canonical(assertion["equals"])
        comparisons.append({
            "path": assertion["path"],
            "matched": observed_json == expected_json,
            "observed_sha256": hashlib.sha256(observed_json.encode()).hexdigest(),
        })
    passed = bool(comparisons) and all(item["matched"] for item in comparisons)
    return {
        "format": FORMAT,
        "state": "passed" if passed else "failed",
        "assertions": comparisons,
    }


def _main() -> int:
    try:
        raw = sys.stdin.read()
        if len(raw.encode("utf-8")) > 64_000:
            raise ValueError("behavior request exceeds size limit")
        request = json.loads(raw)
        if not isinstance(request, dict) or raw != _canonical(request):
            raise ValueError("behavior request must be canonical JSON")
        result = execute(request, root=Path.cwd().resolve())
        sys.stdout.write(_canonical(result))
        return 0 if result["state"] == "passed" else 1
    except Exception as exc:
        failure = {
            "format": FORMAT,
            "state": "error",
            "error_type": type(exc).__name__,
        }
        sys.stdout.write(_canonical(failure))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
