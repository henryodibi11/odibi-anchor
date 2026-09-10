#!/usr/bin/env python
"""Report current Ruff/Pyright debt and block attributable new findings."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPE = ("src", "tests", "tools", "scripts")
PYTHON_TARGET = "3.11"
SCHEMA_VERSION = 1
COMMAND_TIMEOUT_SECONDS = 180
DIAGNOSTIC_LIMIT = 20
PYRIGHT_REQUIRED_MODULES = ("fastmcp", "pydantic", "starlette")
BASELINE_PATHS = {
    "ruff": Path(".quality/ruff-baseline.json"),
    "pyright": Path(".quality/pyright-baseline.json"),
}
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"\b(\d+(?:\.\d+)+)\b")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class RatchetError(RuntimeError):
    """A tool, configuration, or evidence failure that cannot satisfy policy."""


@dataclass(frozen=True)
class ChangedScope:
    """Repository changes used to attribute analyzer diagnostics."""

    ranges: dict[str, tuple[tuple[int, int], ...]]
    created: frozenset[str]
    renamed_from: dict[str, str]


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _normalize_message(message: object) -> str:
    normalized = unicodedata.normalize("NFC", str(message or ""))
    return " ".join(normalized.split())


def _safe_relative_path(value: object, root: Path) -> str:
    raw = str(value or "").replace("\\", "/")
    candidate = Path(raw)
    try:
        if candidate.is_absolute():
            raw = candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise RatchetError(f"diagnostic path is outside the repository: {value!r}") from exc
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise RatchetError(f"diagnostic path is not repository-relative: {value!r}")
    return path.as_posix()


def _fingerprint(tool: str, path: str, rule: str, message: str) -> str:
    payload = json.dumps(
        {"message": message, "path": path, "rule": rule, "tool": tool},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _finding(tool: str, root: Path, *, path: object, rule: object, message: object,
             line: object, column: object) -> dict[str, Any]:
    normalized_path = _safe_relative_path(path, root)
    normalized_rule = _normalize_message(rule)
    normalized_message = _normalize_message(message)
    if not normalized_rule:
        raise RatchetError(f"{tool} diagnostic is missing a stable rule")
    normalized_line = line if isinstance(line, int) and line >= 1 else None
    normalized_column = column if isinstance(column, int) and column >= 1 else None
    return {
        "fingerprint": _fingerprint(tool, normalized_path, normalized_rule, normalized_message),
        "path": normalized_path,
        "rule": normalized_rule,
        "message": normalized_message,
        "line": normalized_line,
        "column": normalized_column,
    }


def _parse_ruff(stdout: str, root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RatchetError("Ruff returned malformed JSON") from exc
    if not isinstance(payload, list):
        raise RatchetError("Ruff JSON result must be a list")
    findings = []
    for item in payload:
        if not isinstance(item, dict):
            raise RatchetError("Ruff JSON contains a non-object diagnostic")
        location = item.get("location")
        if not isinstance(location, dict):
            location = {}
        findings.append(_finding(
            "ruff",
            root,
            path=item.get("filename"),
            rule=item.get("code"),
            message=item.get("message"),
            line=location.get("row"),
            column=location.get("column"),
        ))
    return findings


def _parse_pyright(stdout: str, root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RatchetError("Pyright returned malformed JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("generalDiagnostics"), list):
        raise RatchetError("Pyright JSON result is missing generalDiagnostics")
    findings = []
    for item in payload["generalDiagnostics"]:
        if not isinstance(item, dict):
            raise RatchetError("Pyright JSON contains a non-object diagnostic")
        range_value = item.get("range")
        start = range_value.get("start", {}) if isinstance(range_value, dict) else {}
        line = start.get("line")
        column = start.get("character")
        severity = _normalize_message(item.get("severity")) or "diagnostic"
        findings.append(_finding(
            "pyright",
            root,
            path=item.get("file"),
            rule=item.get("rule") or f"severity:{severity}",
            message=item.get("message"),
            line=line + 1 if isinstance(line, int) and line >= 0 else None,
            column=column + 1 if isinstance(column, int) and column >= 0 else None,
        ))
    return findings


def _run(
    command: Sequence[str],
    *,
    root: Path,
    timeout: int,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            cwd=root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RatchetError(f"required executable is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RatchetError(f"command timed out after {timeout}s: {command[0]}") from exc
    except OSError as exc:
        raise RatchetError(f"command could not run: {command[0]}: {exc}") from exc


def _git(root: Path, *args: str, runner: Runner = subprocess.run) -> str:
    result = _run(["git", *args], root=root, timeout=30, runner=runner)
    if result.returncode != 0:
        diagnostic = _normalize_message((result.stderr or result.stdout)[-500:])
        raise RatchetError(f"Git evidence unavailable for {' '.join(args)}: {diagnostic}")
    return result.stdout


def _resolve_base(root: Path, base_ref: str | None, *, runner: Runner = subprocess.run) -> tuple[str, str]:
    requested = base_ref or os.environ.get("QUALITY_BASE_REF") or "origin/main"
    base_sha = _git(root, "rev-parse", "--verify", f"{requested}^{{commit}}", runner=runner).strip()
    head_sha = _git(root, "rev-parse", "--verify", "HEAD^{commit}", runner=runner).strip()
    merge_base = _git(root, "merge-base", base_sha, head_sha, runner=runner).strip()
    if not _COMMIT_RE.fullmatch(merge_base) or not _COMMIT_RE.fullmatch(head_sha):
        raise RatchetError("Git returned a malformed base or HEAD SHA")
    return merge_base, head_sha


def _parse_name_status(output: str) -> tuple[set[str], dict[str, str]]:
    created: set[str] = set()
    renamed_from: dict[str, str] = {}
    for raw in output.splitlines():
        fields = raw.split("\t")
        status = fields[0] if fields else ""
        if status == "A" and len(fields) == 2:
            created.add(fields[1])
        elif status.startswith("R") and len(fields) == 3:
            renamed_from[fields[2]] = fields[1]
    return created, renamed_from


def _parse_hunks(output: str) -> dict[str, tuple[tuple[int, int], ...]]:
    current_path: str | None = None
    ranges: dict[str, list[tuple[int, int]]] = {}
    for raw in output.splitlines():
        if raw.startswith("+++ "):
            marker = raw[4:]
            current_path = None if marker == "/dev/null" else marker.removeprefix("b/")
            continue
        match = _HUNK_RE.match(raw)
        if match and current_path:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count:
                ranges.setdefault(current_path, []).append((start, start + count - 1))
    return {path: tuple(values) for path, values in sorted(ranges.items())}


def _changed_scope(root: Path, base_sha: str, *, runner: Runner = subprocess.run) -> ChangedScope:
    name_status = _git(root, "diff", "--name-status", "--find-renames", base_sha, "--", runner=runner)
    patch = _git(root, "diff", "--unified=0", "--find-renames", "--no-color", base_sha, "--", runner=runner)
    created, renamed_from = _parse_name_status(name_status)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", runner=runner)
    created.update(path for path in untracked.splitlines() if path)
    return ChangedScope(
        ranges=_parse_hunks(patch),
        created=frozenset(created),
        renamed_from=dict(sorted(renamed_from.items())),
    )


def _scope_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for scope_name in SCOPE:
        scope = root / scope_name
        if not scope.exists():
            continue
        for path in sorted(scope.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _baseline_finding(value: object, tool: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"fingerprint", "path", "rule", "message"}:
        raise RatchetError(f"{tool} baseline contains a malformed finding")
    fingerprint = value.get("fingerprint")
    path = value.get("path")
    rule = value.get("rule")
    message = value.get("message")
    if (
        not isinstance(fingerprint, str)
        or not isinstance(path, str)
        or not isinstance(rule, str)
        or not isinstance(message, str)
    ):
        raise RatchetError(f"{tool} baseline finding fields must be strings")
    normalized_path = PurePosixPath(path)
    if not path or normalized_path.is_absolute() or ".." in normalized_path.parts or "\\" in path:
        raise RatchetError(f"{tool} baseline contains an unsafe path")
    if not _SHA256_RE.fullmatch(fingerprint):
        raise RatchetError(f"{tool} baseline contains a malformed digest")
    if fingerprint != _fingerprint(tool, path, rule, message):
        raise RatchetError(f"{tool} baseline fingerprint does not match its finding")
    return {"fingerprint": fingerprint, "path": path, "rule": rule, "message": message}


def _load_baseline(root: Path, tool: str, tool_version: str) -> tuple[dict[str, Any], str]:
    path = root / BASELINE_PATHS[tool]
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RatchetError(f"{tool} baseline is unreadable or malformed: {path}") from exc
    expected_fields = {
        "schema_version", "tool", "tool_version", "python_target", "scope", "generated_from", "findings",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise RatchetError(f"{tool} baseline has an unknown schema")
    if raw != _canonical_bytes(payload):
        raise RatchetError(f"{tool} baseline is not canonical JSON")
    if payload["schema_version"] != SCHEMA_VERSION or payload["tool"] != tool:
        raise RatchetError(f"{tool} baseline schema/tool mismatch")
    if payload["tool_version"] != tool_version:
        raise RatchetError(
            f"{tool} baseline was generated by {payload['tool_version']}, current tool is {tool_version}"
        )
    if payload["python_target"] != PYTHON_TARGET or payload["scope"] != list(SCOPE):
        raise RatchetError(f"{tool} baseline target or scope mismatch")
    if not isinstance(payload["generated_from"], str) or not _COMMIT_RE.fullmatch(payload["generated_from"]):
        raise RatchetError(f"{tool} baseline generated_from is malformed")
    if not isinstance(payload["findings"], list):
        raise RatchetError(f"{tool} baseline findings must be a list")
    findings = [_baseline_finding(item, tool) for item in payload["findings"]]
    if findings != sorted(findings, key=lambda item: (item["path"], item["rule"], item["fingerprint"])):
        raise RatchetError(f"{tool} baseline findings are not sorted")
    fingerprints = [item["fingerprint"] for item in findings]
    if len(fingerprints) != len(set(fingerprints)):
        raise RatchetError(f"{tool} baseline contains duplicate findings")
    return payload, _sha256_bytes(raw)


def _tool_command(tool: str) -> list[str]:
    if tool == "ruff":
        return ["ruff", "check", "--output-format", "json", *SCOPE]
    return ["pyright", "--pythonpath", sys.executable, "--outputjson", *SCOPE]


def _tool_version(root: Path, tool: str, *, runner: Runner = subprocess.run) -> str:
    result = _run([tool, "--version"], root=root, timeout=30, runner=runner)
    if result.returncode != 0:
        raise RatchetError(f"{tool} --version exited {result.returncode}")
    match = _VERSION_RE.search(result.stdout or result.stderr)
    if not match:
        raise RatchetError(f"{tool} returned a malformed version")
    return match.group(1)


def _require_pyright_environment() -> None:
    missing = [name for name in PYRIGHT_REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise RatchetError(
            "Pyright analysis environment is incomplete; install .[dev] and .[mcp] "
            f"before check/update-baseline (missing: {', '.join(missing)})"
        )


def _tool_findings(root: Path, tool: str, *, runner: Runner = subprocess.run) -> tuple[list[dict[str, Any]], int]:
    if tool == "pyright":
        _require_pyright_environment()
    result = _run(_tool_command(tool), root=root, timeout=COMMAND_TIMEOUT_SECONDS, runner=runner)
    if result.returncode not in {0, 1}:
        diagnostic = _normalize_message((result.stderr or result.stdout)[-500:])
        raise RatchetError(f"{tool} exited {result.returncode}: {diagnostic}")
    if "Config contains unrecognized setting" in result.stderr:
        raise RatchetError(f"{tool} reported an invalid configuration: {_normalize_message(result.stderr)[-500:]}")
    parser = _parse_ruff if tool == "ruff" else _parse_pyright
    return parser(result.stdout, root), result.returncode


def _baseline_alias_fingerprint(tool: str, finding: dict[str, Any], changed: ChangedScope) -> str | None:
    old_path = changed.renamed_from.get(finding["path"])
    if old_path is None:
        return None
    return _fingerprint(tool, old_path, finding["rule"], finding["message"])


def _categorize(
    tool: str,
    findings: list[dict[str, Any]],
    baseline: dict[str, Any],
    changed: ChangedScope,
) -> dict[str, list[dict[str, Any]]]:
    known = {item["fingerprint"] for item in baseline["findings"]}
    categorized: dict[str, list[dict[str, Any]]] = {"existing": [], "introduced": [], "unknown": []}
    for finding in findings:
        path = finding["path"]
        line = finding["line"]
        alias = _baseline_alias_fingerprint(tool, finding, changed)
        if path in changed.created or (
            isinstance(line, int)
            and any(start <= line <= end for start, end in changed.ranges.get(path, ()))
        ):
            category = "introduced"
        elif finding["fingerprint"] in known or alias in known:
            category = "existing"
        else:
            category = "unknown"
        categorized[category].append(finding)
    for values in categorized.values():
        values.sort(key=lambda item: (item["path"], item["line"] or 0, item["rule"], item["fingerprint"]))
    return categorized


def _unavailable_check(
    tool: str,
    *,
    command: list[str],
    started_at: str,
    started: float,
    diagnostic: str,
    scope_digest: str,
) -> dict[str, Any]:
    completed_at = _utc_now()
    return {
        "id": f"static-{tool}",
        "tool": tool,
        "status": "unavailable",
        "exit_code": None,
        "command": command,
        "tool_version": None,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "scope_digest": scope_digest,
        "baseline_digest": None,
        "findings": {"existing": [], "introduced": [], "unknown": []},
        "diagnostics": [diagnostic[:1000]],
    }


def check(
    *,
    root: Path = REPO_ROOT,
    base_ref: str | None = None,
    mode: str = "advisory",
    runner: Runner = subprocess.run,
) -> tuple[dict[str, Any], int]:
    """Run both analyzers and return normalized evidence plus the CLI exit code."""
    if mode not in {"advisory", "ratchet"}:
        raise ValueError(f"unknown mode: {mode}")
    root = root.resolve()
    head_sha: str | None = None
    base_sha: str | None = None
    git_error: str | None = None
    changed = ChangedScope({}, frozenset(), {})
    try:
        base_sha, head_sha = _resolve_base(root, base_ref, runner=runner)
        changed = _changed_scope(root, base_sha, runner=runner)
    except RatchetError as exc:
        git_error = str(exc)
    digest = _scope_digest(root)
    checks = []
    for tool in ("ruff", "pyright"):
        command = _tool_command(tool)
        started = time.monotonic()
        started_at = _utc_now()
        if git_error:
            checks.append(_unavailable_check(
                tool,
                command=command,
                started_at=started_at,
                started=started,
                diagnostic=git_error,
                scope_digest=digest,
            ))
            continue
        try:
            version = _tool_version(root, tool, runner=runner)
            baseline, baseline_digest = _load_baseline(root, tool, version)
            findings, tool_exit_code = _tool_findings(root, tool, runner=runner)
            categorized = _categorize(tool, findings, baseline, changed)
            status = "fail" if categorized["introduced"] or categorized["unknown"] else "pass"
            completed_at = _utc_now()
            checks.append({
                "id": f"static-{tool}",
                "tool": tool,
                "status": status,
                "exit_code": tool_exit_code,
                "command": command,
                "tool_version": version,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "scope_digest": digest,
                "baseline_digest": baseline_digest,
                "findings": categorized,
                "diagnostics": [],
            })
        except RatchetError as exc:
            checks.append(_unavailable_check(
                tool,
                command=command,
                started_at=started_at,
                started=started,
                diagnostic=str(exc),
                scope_digest=digest,
            ))
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "scope": list(SCOPE),
        "checks": checks,
    }
    if any(item["status"] == "unavailable" for item in checks):
        return result, 2
    if mode == "ratchet" and any(item["status"] == "fail" for item in checks):
        return result, 1
    return result, 0


def _baseline_payload(tool: str, version: str, head_sha: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    unique = {
        finding["fingerprint"]: {
            "fingerprint": finding["fingerprint"],
            "path": finding["path"],
            "rule": finding["rule"],
            "message": finding["message"],
        }
        for finding in findings
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "tool_version": version,
        "python_target": PYTHON_TARGET,
        "scope": list(SCOPE),
        "generated_from": head_sha,
        "findings": sorted(unique.values(), key=lambda item: (item["path"], item["rule"], item["fingerprint"])),
    }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def update_baseline(
    tool: str,
    *,
    root: Path = REPO_ROOT,
    runner: Runner = subprocess.run,
) -> dict[str, int]:
    """Replace one baseline after a clean, explicit invocation."""
    root = root.resolve()
    if _git(root, "status", "--porcelain", "--untracked-files=all", runner=runner).strip():
        raise RatchetError("update-baseline requires a clean Git worktree")
    head_sha = _git(root, "rev-parse", "--verify", "HEAD^{commit}", runner=runner).strip()
    version = _tool_version(root, tool, runner=runner)
    findings, _ = _tool_findings(root, tool, runner=runner)
    payload = _baseline_payload(tool, version, head_sha, findings)
    destination = root / BASELINE_PATHS[tool]
    previous: set[str] = set()
    if destination.exists():
        old, _ = _load_baseline(root, tool, version)
        previous = {item["fingerprint"] for item in old["findings"]}
    current = {item["fingerprint"] for item in payload["findings"]}
    _atomic_write(destination, _canonical_bytes(payload))
    return {"additions": len(current - previous), "removals": len(previous - current), "findings": len(current)}


def _print_text(result: dict[str, Any]) -> None:
    print(f"Quality ratchet ({result['mode']})")
    print(f"base={result['base_sha'] or 'unavailable'} head={result['head_sha'] or 'unavailable'}")
    for item in result["checks"]:
        findings = item["findings"]
        print(
            f"{item['tool']}: {item['status']} — "
            f"existing={len(findings['existing'])}, introduced={len(findings['introduced'])}, "
            f"unknown={len(findings['unknown'])}"
        )
        for diagnostic in item["diagnostics"][:DIAGNOSTIC_LIMIT]:
            print(f"  {diagnostic}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="run Ruff and Pyright against checked-in baselines")
    check_parser.add_argument("--base-ref")
    check_parser.add_argument("--mode", choices=("advisory", "ratchet"), default="advisory")
    check_parser.add_argument("--format", choices=("text", "json"), default="text")
    update_parser = commands.add_parser("update-baseline", help="replace one baseline from a clean checkout")
    update_parser.add_argument("--tool", choices=("ruff", "pyright"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "update-baseline":
        try:
            summary = update_baseline(args.tool)
        except RatchetError as exc:
            print(f"quality ratchet unavailable: {exc}", file=sys.stderr)
            return 2
        print(
            f"{args.tool} baseline updated: {summary['findings']} findings "
            f"(+{summary['additions']}/-{summary['removals']})"
        )
        return 0
    result, exit_code = check(base_ref=args.base_ref, mode=args.mode)
    if args.format == "json":
        sys.stdout.buffer.write(_canonical_bytes(result))
    else:
        _print_text(result)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
