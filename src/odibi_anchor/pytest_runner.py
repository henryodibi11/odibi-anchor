"""Canonical, hermetic pytest subprocess execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUMMARY_ENV = "ODIBI_ANCHOR_PYTEST_SUMMARY"
_CANONICAL_PLUGIN_NAME = "odibi_anchor.pytest_runner"
_GIT_CONFIG = (
    ("commit.gpgSign", "false"),
    ("user.name", "Odibi Anchor Tests"),
    ("user.email", "tests@odibi-anchor.invalid"),
)


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return child-only Python and Git settings without changing Git config."""
    env = dict(os.environ if base is None else base)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    start = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate(_GIT_CONFIG):
        index = start + offset
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(_GIT_CONFIG))
    env.update(
        GIT_AUTHOR_NAME="Odibi Anchor Tests",
        GIT_AUTHOR_EMAIL="tests@odibi-anchor.invalid",
        GIT_COMMITTER_NAME="Odibi Anchor Tests",
        GIT_COMMITTER_EMAIL="tests@odibi-anchor.invalid",
    )
    return env


def run_pytest(
    pytest_args: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout: float | None = None,
    capture_output: bool = False,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str] | None]:
    """Run pytest through this interpreter and return its bounded hook summary."""
    started = time.monotonic()
    summary_path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix="anchor-pytest-", suffix=".json")
        os.close(fd)
        summary_path = Path(raw_path)
        summary_path.chmod(0o600)
        env = child_environment()
        env[SUMMARY_ENV] = str(summary_path)
        cmd = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "-p",
            "no:cacheprovider",
            *pytest_args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            summary = _empty_summary(-1, time.monotonic() - started, timed_out=True)
            proc = subprocess.CompletedProcess(cmd, -1, exc.stdout or "", exc.stderr or "")
            return summary, proc

        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        summary = _empty_summary(proc.returncode, time.monotonic() - started)
        for field in ("passed", "failed", "errors", "skipped"):
            value = loaded.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                summary[field] = value
        if proc.returncode != 0 and not any(summary[field] for field in ("failed", "errors")):
            summary["errors"] = 1
        return summary, proc
    finally:
        if summary_path is not None:
            summary_path.unlink(missing_ok=True)


def _empty_summary(exit_code: int, duration_s: float, *, timed_out: bool = False) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "exit_code": exit_code,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "duration_s": round(max(0.0, duration_s), 3),
        "timed_out": timed_out,
    }


class _SummaryPlugin:
    def __init__(self) -> None:
        self.counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        self.started = time.monotonic()

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.skipped:
            self.counts["skipped"] += 1
        elif report.when == "call":
            self.counts["passed" if report.passed else "failed"] += 1
        elif report.failed:
            self.counts["errors"] += 1

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.counts["errors"] += 1
        elif report.skipped:
            self.counts["skipped"] += 1

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        path = os.environ.get(SUMMARY_ENV)
        if not path:
            return
        summary = _empty_summary(int(exitstatus), time.monotonic() - self.started)
        summary.update(self.counts)
        Path(path).write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")


def pytest_configure(config: Any) -> None:
    """Register the supported lifecycle-hook summary plugin."""
    config.pluginmanager.register(_SummaryPlugin(), "odibi-anchor-summary")


def _child_main() -> int:
    """Run pytest with this exact loaded file as its structured-summary plugin."""
    child_module = sys.modules[__name__]
    sys.modules[_CANONICAL_PLUGIN_NAME] = child_module
    child_module.__name__ = _CANONICAL_PLUGIN_NAME

    import pytest

    # Executing this file pins plugin provenance but initially makes its package
    # directory sys.path[0]. Restore `python -m pytest` target import semantics
    # without discarding any safe-path caller's inherited PYTHONPATH entries.
    cwd = Path.cwd().resolve()
    runner_parent = Path(__file__).resolve().parent
    safe_path = bool(getattr(sys.flags, "safe_path", False))
    first_is_runner_parent = (
        bool(sys.path)
        and isinstance(sys.path[0], str)
        and Path(sys.path[0] or ".").resolve() == runner_parent
    )
    if not safe_path and first_is_runner_parent:
        sys.path[0] = str(cwd)
    elif not any(
        isinstance(entry, str) and Path(entry or ".").resolve() == cwd
        for entry in sys.path
    ):
        sys.path.insert(0, str(cwd))
    return int(pytest.main(sys.argv[1:], plugins=[child_module]))


if __name__ == "__main__":
    raise SystemExit(_child_main())
