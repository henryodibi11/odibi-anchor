#!/usr/bin/env python
"""Run the canonical local verification path for odibi_anchor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 1_200
DIAGNOSTIC_LIMIT = 4_000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _environment() -> dict[str, str]:
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def run_script(name: str) -> int:
    """Run a named repository script and return its exit code (legacy API)."""
    path = SCRIPTS_DIR / name
    result = subprocess.run([sys.executable, str(path)], env=_environment(), check=False)
    return result.returncode


def _bounded_diagnostic(value: str) -> str:
    return value[-DIAGNOSTIC_LIMIT:].replace(str(Path.home()), "<home>")


def _parse_json_output(stdout: str, check_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{check_id} returned malformed JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"{check_id} returned an unknown result schema")
    return payload


def _test_summary(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("canonical test launcher returned no summary")
    payload = json.loads(lines[-1])
    required = {"schema_version", "exit_code", "passed", "failed", "errors", "skipped", "timed_out"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("canonical test launcher returned a malformed summary")
    return payload


def _quality_summary(payload: dict[str, Any]) -> dict[str, Any]:
    checks = payload.get("checks")
    if not isinstance(checks, list) or {item.get("tool") for item in checks if isinstance(item, dict)} != {
        "ruff", "pyright",
    }:
        raise ValueError("quality ratchet result is missing Ruff/Pyright checks")
    return {
        "mode": payload.get("mode"),
        "base_sha": payload.get("base_sha"),
        "head_sha": payload.get("head_sha"),
        "checks": [
            {
                "id": item.get("id"),
                "tool": item.get("tool"),
                "status": item.get("status"),
                "tool_version": item.get("tool_version"),
                "scope_digest": item.get("scope_digest"),
                "baseline_digest": item.get("baseline_digest"),
                "finding_counts": {
                    category: len(item.get("findings", {}).get(category, []))
                    for category in ("existing", "introduced", "unknown")
                },
                "diagnostics": item.get("diagnostics", [])[:5],
            }
            for item in checks
        ],
    }


def _distribution_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") not in {"pass", "fail", "unavailable"}:
        raise ValueError("distribution verifier returned an unknown status")
    checks = payload.get("checks")
    observations = payload.get("observations")
    if not isinstance(checks, list) or not isinstance(observations, list):
        raise ValueError("distribution verifier returned malformed checks/observations")
    return {
        "status": payload["status"],
        "checks": [
            {"id": item.get("id"), "status": item.get("status"), "required": item.get("required")}
            for item in checks
            if isinstance(item, dict)
        ],
        "observations": [
            {"id": item.get("id"), "status": item.get("status"), "advisory": item.get("advisory")}
            for item in observations
            if isinstance(item, dict)
        ],
        "claims": payload.get("claims", []),
    }


def _execute(
    check_id: str,
    label: str,
    script: str,
    args: Sequence[str],
    *,
    timeout: int,
) -> dict[str, Any]:
    path = SCRIPTS_DIR / script
    command = [sys.executable, str(path), *args]
    started_at = _utc_now()
    started = time.monotonic()
    base = {
        "id": check_id,
        "label": label,
        "command": command,
        "started_at": started_at,
    }
    if not path.is_file():
        return {
            **base,
            "status": "unavailable",
            "exit_code": None,
            "completed_at": _utc_now(),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "diagnostic": f"required script is unavailable: {script}",
        }
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=_environment(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            **base,
            "status": "unavailable",
            "exit_code": None,
            "completed_at": _utc_now(),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "diagnostic": f"command timed out after {timeout}s",
        }
    except OSError as exc:
        return {
            **base,
            "status": "unavailable",
            "exit_code": None,
            "completed_at": _utc_now(),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "diagnostic": f"command could not run: {exc}",
        }
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    status = "pass" if result.returncode == 0 else "fail"
    details: dict[str, Any] | None = None
    try:
        if check_id == "quality-ratchet":
            payload = _parse_json_output(result.stdout, check_id)
            details = _quality_summary(payload)
            if any(item["status"] == "unavailable" for item in details["checks"]):
                status = "unavailable"
        elif check_id == "tests":
            details = _test_summary(result.stdout)
            if details["timed_out"] or details["exit_code"] != result.returncode:
                status = "unavailable"
            elif details["failed"] or details["errors"]:
                status = "fail"
        elif check_id == "distribution":
            payload = _parse_json_output(result.stdout, check_id)
            details = _distribution_summary(payload)
            status = details["status"]
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        status = "unavailable"
        details = {"parse_error": str(exc)}
    completed = {
        **base,
        "status": status,
        "exit_code": result.returncode,
        "completed_at": _utc_now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    if details is not None:
        completed["details"] = details
    if status != "pass":
        completed["diagnostic"] = _bounded_diagnostic(result.stderr or result.stdout)
    return completed


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("advisory", "ratchet"), default="advisory")
    parser.add_argument("--base-ref")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run hard local checks plus the configured advisory/ratchet static policy."""
    args = _parser().parse_args(argv)
    if args.timeout < 1:
        raise SystemExit("--timeout must be at least 1 second")
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     odibi_anchor — Full Verification Suite         ║")
    print("╚══════════════════════════════════════════════════════════╝")
    started_at = _utc_now()
    started = time.monotonic()
    quality_args = ["check", "--mode", args.mode, "--format", "json"]
    if args.base_ref:
        quality_args.extend(["--base-ref", args.base_ref])
    definitions = [
        ("forbidden-imports", "1. No forbidden framework imports", "check_no_datakit_imports.py", []),
        ("quality-ratchet", "2. Ruff/Pyright quality ratchet", "quality_ratchet.py", quality_args),
        ("tests", "3. Pytest suite", "run_tests.py", ["tests/"]),
        ("output-contracts", "4. Output contracts", "verify_outputs.py", []),
        ("distribution", "5. Distribution build/install", "verify_distribution.py", ["--format", "json"]),
    ]
    checks = []
    for check_id, label, script, command_args in definitions:
        print(f"\n{'─' * 60}\n  {label}\n{'─' * 60}")
        check = _execute(check_id, label, script, command_args, timeout=args.timeout)
        checks.append(check)
        print(f"  {check['status'].upper()}")
    passed = all(check["status"] == "pass" for check in checks)
    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": args.mode,
        "status": "pass" if passed else "fail",
        "started_at": started_at,
        "completed_at": _utc_now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "checks": checks,
    }
    if args.json_out:
        _atomic_json(args.json_out, report)
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║                    SUMMARY                              ║")
    for check in checks:
        print(f"  {check['label']}: {check['status'].upper()}")
    print("╚══════════════════════════════════════════════════════════╝")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
