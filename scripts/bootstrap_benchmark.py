"""Measure managed Odibi Anchor bootstraps and print their phase timings as JSON.

Run on the target compute (for example a Databricks notebook driver)::

    python scripts/bootstrap_benchmark.py \
        --launcher /Workspace/Users/<user>/.assistant/agent_bootstrap.py \
        --project-id <project-id> --runs 3

Each run executes the managed launcher exactly once in a fresh Python process, exactly
as ``runpy.run_path`` does in a notebook, and reports ``STARTUP_PACKET["timings"]``.
The script performs no writes of its own: the only effects are the launcher's normal
bootstrap effects (host-guidance reconciliation, restore of absent local state, route
binding). It never deletes or copies state, so it cannot manufacture a cold start.

Classification is observed, not forced: a run whose packet reports a performed restore
is ``cold_state`` (local state was absent on this compute identity); every other run is
``warm_state``. To measure a cold bootstrap, run on a freshly attached compute or a new
serverless session; the first run then restores and the following runs are warm.

``--isolation in-process`` runs a single bootstrap in the current process for runtimes
whose credentials are not inherited by child processes. It honors the
one-bootstrap-per-process rule, so it accepts only ``--runs 1``.
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_MARKER = "ODIBI_ANCHOR_BENCHMARK="

_CHILD = r"""
import json, runpy, sys, time
started = time.perf_counter()
launcher, init_globals = sys.argv[1], json.loads(sys.argv[2])
try:
    namespace = runpy.run_path(launcher, init_globals=init_globals)
    packet = namespace["STARTUP_PACKET"]
    record = {"ok": True, "packet": {key: packet.get(key) for key in (
        "status", "project_id", "host_id", "guidance", "restore", "timings")}}
except BaseException as exc:
    record = {
        "ok": False, "error_type": type(exc).__name__, "message": str(exc)[:4000],
        "error_code": getattr(exc, "error_code", None),
        "context": getattr(exc, "context", None),
        "timings": getattr(exc, "bootstrap_timings", None),
    }
record["in_process_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
print("ODIBI_ANCHOR_BENCHMARK=" + json.dumps(record, sort_keys=True, default=str), flush=True)
"""


def _classification(record: dict[str, Any]) -> str | None:
    packet = record.get("packet")
    if not record.get("ok") or not isinstance(packet, dict):
        return None
    restore = packet.get("restore") or {}
    performed = restore.get("action") == "created" or restore.get("status") == "restored"
    return "cold_state" if performed else "warm_state"


def _run_subprocess(launcher: str, init_globals: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, launcher, json.dumps(init_globals)],
            capture_output=True, text=True, timeout=timeout, env=dict(os.environ), check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "BenchmarkRunTimeout",
                "message": f"run exceeded {timeout:g} s", "process_wall_ms": timeout * 1000.0}
    lines = [line for line in (completed.stdout or "").splitlines() if line.startswith(_MARKER)]
    record: dict[str, Any] = (
        json.loads(lines[-1].removeprefix(_MARKER)) if lines else {
            "ok": False, "error_type": "NoBenchmarkRecord",
            "message": (completed.stderr or "")[-4000:],
        }
    )
    record["exit_code"] = completed.returncode
    record["process_wall_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return record


def _run_in_process(launcher: str, init_globals: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        namespace = runpy.run_path(launcher, init_globals=init_globals)
        packet = namespace["STARTUP_PACKET"]
        record: dict[str, Any] = {"ok": True, "packet": {key: packet.get(key) for key in (
            "status", "project_id", "host_id", "guidance", "restore", "timings")}}
    except Exception as exc:
        record = {
            "ok": False, "error_type": type(exc).__name__, "message": str(exc)[:4000],
            "error_code": getattr(exc, "error_code", None),
            "context": getattr(exc, "context", None),
            "timings": getattr(exc, "bootstrap_timings", None),
        }
    record["in_process_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return record


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    samples: dict[str, list[float]] = {}
    for run in runs:
        timings = (run.get("packet") or {}).get("timings") or run.get("timings") or {}
        for entry in timings.get("phases", []):
            samples.setdefault(entry["phase"], []).append(float(entry["elapsed_ms"]))
            for child in entry.get("sub_phases", []):
                samples.setdefault(f"{entry['phase']}.{child['phase']}", []).append(
                    float(child["elapsed_ms"])
                )
    return {
        name: {"count": len(values), "min_ms": min(values),
               "median_ms": statistics.median(values), "max_ms": max(values)}
        for name, values in samples.items()
    }


def run_benchmark(
    *, launcher: str, project_id: str, runs: int = 3, isolation: str = "subprocess",
    portfolio_config: str | None = None, run_timeout: float = 900.0,
) -> dict[str, Any]:
    path = Path(launcher)
    if not path.is_absolute() or path.name != "agent_bootstrap.py":
        raise ValueError("launcher must be the absolute path of .assistant/agent_bootstrap.py")
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if isolation == "in-process" and runs != 1:
        raise ValueError("in-process isolation bootstraps once per process; use --runs 1")
    init_globals: dict[str, Any] = {"ANCHOR_PROJECT_ID": project_id}
    if portfolio_config is not None:
        init_globals["ANCHOR_PORTFOLIO_CONFIG"] = portfolio_config
    results = []
    for index in range(1, runs + 1):
        record = (
            _run_in_process(launcher, init_globals)
            if isolation == "in-process"
            else _run_subprocess(launcher, init_globals, run_timeout)
        )
        results.append({"run": index, "isolation": isolation,
                        "classification": _classification(record), **record})
    return {
        "kind": "odibi_anchor_bootstrap_benchmark",
        "launcher": launcher,
        "project_id": project_id,
        "runs": results,
        "phase_summary": _summary(results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure managed Odibi Anchor bootstrap phase timings.")
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--isolation", choices=("subprocess", "in-process"), default="subprocess")
    parser.add_argument("--portfolio-config")
    parser.add_argument("--run-timeout", type=float, default=900.0)
    arguments = parser.parse_args(argv)
    report = run_benchmark(
        launcher=arguments.launcher, project_id=arguments.project_id, runs=arguments.runs,
        isolation=arguments.isolation, portfolio_config=arguments.portfolio_config,
        run_timeout=arguments.run_timeout,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if all(run.get("ok") for run in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
