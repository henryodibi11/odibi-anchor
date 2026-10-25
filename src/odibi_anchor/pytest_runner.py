"""Canonical, hermetic pytest subprocess execution."""

from __future__ import annotations

import contextlib
import json
import locale
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

SCHEMA_VERSION = 1
SUMMARY_ENV = "ODIBI_ANCHOR_PYTEST_SUMMARY"
#: Seconds after which a `start_pytest` child terminates itself. Set only by
#: `start_pytest` (its timeout plus `CHILD_DEADLINE_GRACE_S`), so a run whose
#: handle is never polled again still has a finite lifetime; the parent's poll
#: enforces the exact budget first in the normal case.
DEADLINE_ENV = "ODIBI_ANCHOR_PYTEST_DEADLINE_S"
CHILD_DEADLINE_GRACE_S = 5.0
CLOSE_GRACE_S = 1.0
# Anchor routing variables (ANCHOR_HOME, ANCHOR_MEMORY_DB, ANCHOR_PROJECT_ID,
# ANCHOR_PROJECT_ROOT, ...). SUMMARY_ENV starts with ODIBI_ANCHOR_ and is unaffected.
ANCHOR_ENV_PREFIX = "ANCHOR_"
#: `ANCHOR_` names that are deliberate test-suite controls rather than runtime
#: routing, and so must survive into the child. Stripping the whole prefix removed
#: these too, which silently disabled a *required* control: the quality workflow
#: exports ANCHOR_REQUIRE_INSTALLED_QUALIFICATION=1 and the installed-distribution
#: suite reads it, so the qualification it demands quietly stopped being enforced.
#: ANCHOR_OFFLINE_WHEELHOUSE is the same shape — it points the suite at a local
#: wheel directory, and losing it turns an offline run into a network install.
#: Nothing here selects a project, home, database or target, which is what makes
#: these safe to keep while routing is still removed.
PRESERVED_ENV_NAMES = frozenset({
    "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION",
    "ANCHOR_OFFLINE_WHEELHOUSE",
})
#: Owner-configured interpreter for project tests, set only through the portfolio
#: (``projects.<id>.test_python.<host>``) and delivered in the bound runtime environment.
TEST_PYTHON_ENV = "ANCHOR_TEST_PYTHON"
#: This host's owner-named test environments (``projects.<id>.test_environments.<host>``)
#: as JSON ``{name: {kind, python?, distro?, env?}}``, delivered like TEST_PYTHON_ENV.
TEST_ENVIRONMENTS_ENV = "ANCHOR_TEST_ENVIRONMENTS"
TEST_ENVIRONMENT_KINDS = ("local", "wsl", "runtime")
_PROBE = (
    "import importlib.util, json, sys; import pytest; "
    "print(json.dumps({'pytest_timeout': importlib.util.find_spec('pytest_timeout') is not None}))"
)
_ENVIRONMENT = (
    "import importlib.metadata, json, sys; print(json.dumps({'python': sys.version, "
    "'distributions': sorted([[d.metadata['Name'], d.version] for d in importlib.metadata.distributions()], "
    "key=lambda item: (item[0] or '', item[1]))}))"
)
COUNT_FIELDS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
MAX_SKIP_REASONS = 20
MAX_SKIP_REASON_CHARS = 300
_CANONICAL_PLUGIN_NAME = "odibi_anchor.pytest_runner"
_GIT_CONFIG = (
    ("commit.gpgSign", "false"),
    ("user.name", "Odibi Anchor Tests"),
    ("user.email", "tests@odibi-anchor.invalid"),
)


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return child-only Python and Git settings without changing Git config.

    Anchor's own routing variables are stripped. `anchor("test")` runs inside a live
    runtime, so inheriting them pointed the suite at the operator's real home,
    memory database and project: tests that resolve Anchor state from the
    environment bound to that project instead of their own fixtures, wrote foreign
    task windows into the operator's database, and failed in ways that do not
    reproduce under plain pytest. Stripping the prefix gives the child the same
    environment a contributor's `pytest` invocation sees. `ODIBI_ANCHOR_` names,
    including the summary hand-off below, do not match the prefix and are kept.

    `PRESERVED_ENV_NAMES` are exempt: they are suite controls, not routing, and
    stripping them silently disabled a required qualification gate.
    """
    env = dict(os.environ if base is None else base)
    for name in [
        key for key in env
        if key.startswith(ANCHOR_ENV_PREFIX) and key not in PRESERVED_ENV_NAMES
    ]:
        del env[name]
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


def named_environment(name: str) -> dict[str, Any]:
    """Resolve a criterion's named test environment on this host; refuse an unknown name."""
    raw = os.environ.get(TEST_ENVIRONMENTS_ENV) or "{}"
    try:
        configured = json.loads(raw)
    except ValueError:
        configured = None
    if not isinstance(configured, dict):
        raise ValueError(f"{TEST_ENVIRONMENTS_ENV} is not a JSON object; restart and rebootstrap "
                         "so the portfolio delivers it")
    selected = configured.get(name)
    if not isinstance(selected, dict):
        names = ", ".join(sorted(configured)) or "none"
        raise ValueError(
            f"test environment {name!r} is not configured on this host (configured: {names}). "
            "Add projects.<id>.test_environments.<host>.<name> to the portfolio, then restart "
            "and rebootstrap; or remove the criterion's environment to use the default interpreter."
        )
    kind = selected.get("kind")
    python = sys.executable if kind == "runtime" else selected.get("python")
    variables = selected.get("env") or {}
    if (kind not in TEST_ENVIRONMENT_KINDS or not isinstance(python, str) or not python
            or (kind == "wsl" and not isinstance(selected.get("distro"), str))
            or not isinstance(variables, dict)
            or not all(isinstance(value, str) for value in variables.values())):
        raise ValueError(f"test environment {name!r} is malformed in {TEST_ENVIRONMENTS_ENV}; "
                         "fix projects.<id>.test_environments.<host>.<name> in the portfolio")
    environment = {"name": name, "kind": kind, "python": python, "env": dict(variables)}
    if kind == "wsl":
        environment["distro"] = selected["distro"]
    return environment


def wsl_path(path: str | os.PathLike[str]) -> str:
    """Translate an absolute Windows path to its WSL path (C:\\x -> /mnt/c/x).

    ``\\\\wsl$\\<distro>\\`` and ``\\\\wsl.localhost\\<distro>\\`` paths map to the distro root;
    anything else (relative paths, POSIX paths, options) passes through unchanged.
    """
    text = os.fspath(path)
    windows = PureWindowsPath(text)
    if not windows.is_absolute():
        return text
    rest = "/".join(windows.parts[1:])
    drive = windows.drive
    if len(drive) == 2 and drive[1] == ":":
        return f"/mnt/{drive[0].lower()}/{rest}"
    if drive.lower().startswith(("\\\\wsl$\\", "\\\\wsl.localhost\\")):
        return f"/{rest}"
    return text


def environment_launch(
    environment: dict[str, Any], argv: Sequence[str], *, cwd: str | os.PathLike[str] | None = None,
    variables: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return the child environment and argv that run ``argv`` in a named test environment.

    A leading ``"python"`` is the environment's interpreter. Local and runtime environments
    run directly with their variables added to the hermetic child environment. WSL runs
    ``wsl.exe -d <distro> --cd <cwd> -e env K=V ... argv``: Windows variables do not cross
    into the distro, so the hermetic settings and variables are passed explicitly, and
    absolute Windows paths in argv and variable values are translated.
    """
    env = child_environment()
    passed = {**environment.get("env", {}), **(variables or {})}
    command = list(argv)
    if command and command[0] == "python":
        command[0] = environment["python"]
    if environment["kind"] != "wsl":
        env.update(passed)
        return env, command
    passed = {**child_environment({}), **passed}
    prefix = ["wsl.exe", "-d", environment["distro"]]
    if cwd is not None:
        prefix += ["--cd", wsl_path(cwd)]
    return env, [*prefix, "-e", "env", *(f"{key}={wsl_path(value)}" for key, value in passed.items()),
                 *(wsl_path(item) for item in command)]


def _run_configured(python: str, code: str,
                    environment: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    env, cmd = (child_environment(), [python, "-B", "-c", code]) if environment is None else (
        environment_launch(environment, ["python", "-B", "-c", code]))
    return subprocess.run(
        cmd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
    )


def probe_environment(environment: dict[str, Any]) -> dict[str, Any]:
    """Check that pytest imports in a named test environment; report pytest-timeout."""
    fix = (f"Fix projects.<id>.test_environments.<host>.{environment['name']} in the portfolio, "
           "then restart and rebootstrap.")
    try:
        probe = _run_configured(environment["python"], _PROBE, environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"test environment {environment['name']!r} could not run: {exc}. {fix}") from exc
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()[-1:] or ["no output"]
        raise ValueError(f"pytest is not importable in test environment {environment['name']!r} "
                         f"({detail[0]}). Install pytest there, or {fix[0].lower()}{fix[1:]}")
    return {"executable": environment["python"], **json.loads(probe.stdout.strip().splitlines()[-1])}


def probe_test_python() -> dict[str, Any] | None:
    """Check the owner-configured test interpreter, or return None when none is configured.

    The runner needs only the standard library and pytest in that interpreter; Anchor
    itself need not be installed there.
    """
    raw = os.environ.get(TEST_PYTHON_ENV)
    if not raw:
        return None
    fix = (f"Set projects.<id>.test_python.<host> in the portfolio to an executable "
           f"interpreter, then restart and rebootstrap ({TEST_PYTHON_ENV}={raw}).")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"the configured test interpreter must be an absolute path. {fix}")
    if not path.is_file():
        raise ValueError(f"the configured test interpreter does not exist or is not a file. {fix}")
    if not os.access(path, os.X_OK):
        raise ValueError(f"the configured test interpreter is not executable. {fix}")
    try:
        probe = _run_configured(raw, _PROBE)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"the configured test interpreter could not run: {exc}. {fix}") from exc
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()[-1:] or ["no output"]
        raise ValueError(
            f"pytest is not importable in the configured test interpreter ({detail[0]}). "
            f"Install pytest into that environment, or {fix[0].lower()}{fix[1:]}"
        )
    return {"executable": raw, **json.loads(probe.stdout.strip().splitlines()[-1])}


def configured_environment(python: str, environment: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe the configured interpreter or named environment by running it, never Anchor's own."""
    result = _run_configured(python, _ENVIRONMENT, environment)
    if result.returncode != 0:
        raise ValueError(f"the configured test interpreter could not report its distributions: "
                         f"{(result.stderr or '').strip()[-300:]}")
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    return {"python": observed["python"], "executable": python,
            "distributions": observed["distributions"],
            "source": TEST_PYTHON_ENV if environment is None else TEST_ENVIRONMENTS_ENV}


def run_pytest(
    pytest_args: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout: float | None = None,
    capture_output: bool = False,
    python: str | None = None,
    environment: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str] | None]:
    """Run pytest through this interpreter, ``python`` or a named ``environment``; return its summary."""
    started = time.monotonic()
    summary_path: Path | None = None
    try:
        summary_path = _new_summary_path()
        env, cmd = _launch_spec(pytest_args, summary_path, python, environment, cwd)
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
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
            proc = subprocess.CompletedProcess(cmd, -1, stdout, stderr)
            return summary, proc

        loaded = _read_summary(summary_path)
        return _completed_summary(proc.returncode, time.monotonic() - started, loaded), proc
    finally:
        if summary_path is not None:
            summary_path.unlink(missing_ok=True)


def start_pytest(
    pytest_args: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout: float | None = None,
    capture_output: bool = False,
    python: str | None = None,
    environment: dict[str, Any] | None = None,
) -> PytestProcess:
    """Launch the same hermetic pytest child as `run_pytest` without waiting for it.

    Returns a process-local handle; poll it with `wait()` and always `close()` it.
    The handle starts no threads and touches no shared state. Captured output is
    spooled to anonymous temporary files so an unpolled child never blocks on a
    full pipe. `timeout` is the total budget measured from launch: it is enforced
    whenever the handle is polled, and the child also carries a slightly later
    self-terminating deadline so it has a finite lifetime even if nobody polls.
    """
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")
    return PytestProcess(pytest_args, cwd=cwd, timeout=timeout, capture_output=capture_output,
                         python=python, environment=environment)


class PytestProcess:
    """Handle for one pytest child started by `start_pytest`.

    `wait(wait_seconds)` returns the same `(summary, CompletedProcess)` tuple as
    `run_pytest` once the run is finished (or has exceeded its total timeout, in
    which case the child is killed and the summary reports `timed_out`), and
    `None` while it is still running. `wait_seconds=None` blocks until the run
    finishes or times out; `0` is a non-blocking poll. A finished result is cached
    and returned again by later calls. `close()` terminates and reaps a running
    child and removes every temporary file the handle owns; it is idempotent.
    """

    def __init__(
        self,
        pytest_args: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        timeout: float | None,
        capture_output: bool,
        python: str | None = None,
        environment: dict[str, Any] | None = None,
    ) -> None:
        self.timeout = timeout
        self.capture_output = capture_output
        self._result: tuple[dict[str, Any], subprocess.CompletedProcess[str]] | None = None
        self._closed = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout: Any = None
        self._stderr: Any = None
        self._summary_path: Path | None = _new_summary_path()
        try:
            env, self.args = _launch_spec(pytest_args, self._summary_path, python, environment, cwd)
            if timeout is not None:
                env[DEADLINE_ENV] = repr(float(timeout) + CHILD_DEADLINE_GRACE_S)
            if capture_output:
                # Owned for the handle's lifetime; closed by `_cleanup_files`.
                self._stdout = tempfile.TemporaryFile(prefix="anchor-pytest-out-")  # noqa: SIM115
                self._stderr = tempfile.TemporaryFile(prefix="anchor-pytest-err-")  # noqa: SIM115
            # Own process group/session, so termination also reaches grandchildren.
            posix = os.name == "posix"
            self.started = time.monotonic()
            proc = subprocess.Popen(
                self.args,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=self._stdout,
                stderr=self._stderr,
                start_new_session=posix,
                creationflags=0 if posix else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except BaseException:
            self._cleanup_files()
            raise
        self._proc = proc
        self.pid = proc.pid

    @property
    def done(self) -> bool:
        """True once a result has been produced."""
        return self._result is not None

    def wait(
        self, wait_seconds: float | None = None
    ) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]] | None:
        if self._result is not None:
            return self._result
        if self._closed or self._proc is None:
            raise RuntimeError("pytest process handle is closed")
        if wait_seconds is not None and wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative")
        budget = wait_seconds
        remaining = self._remaining()
        if remaining is not None:
            budget = remaining if budget is None else min(budget, remaining)
        try:
            returncode = self._proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            returncode = None
        if returncode is None:
            remaining = self._remaining()
            if remaining is None or remaining > 0:
                return None
            return self._finish_timed_out()
        return self._finish_exited(returncode)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                self._signal_group(terminate=True)
                try:
                    proc.wait(timeout=CLOSE_GRACE_S)
                except subprocess.TimeoutExpired:
                    self._signal_group(terminate=False)
                    proc.wait()
        finally:
            self._cleanup_files()

    def __enter__(self) -> PytestProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _remaining(self) -> float | None:
        if self.timeout is None:
            return None
        return self.timeout - (time.monotonic() - self.started)

    def _signal_group(self, *, terminate: bool) -> None:
        # Only called while the leader is unreaped, so its pid/pgid cannot be reused.
        proc = self._proc
        assert proc is not None
        try:
            if os.name == "posix":
                import signal

                os.killpg(proc.pid, signal.SIGTERM if terminate else signal.SIGKILL)
            elif terminate:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(OSError):
                proc.terminate() if terminate else proc.kill()

    def _read_output(self) -> tuple[str, str]:
        return _decode_spool(self._stdout), _decode_spool(self._stderr)

    def _finish_timed_out(self) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
        proc = self._proc
        assert proc is not None
        if proc.poll() is None:
            self._signal_group(terminate=False)
            proc.wait()
        summary = _empty_summary(-1, time.monotonic() - self.started, timed_out=True)
        stdout, stderr = self._read_output()
        return self._store(summary, subprocess.CompletedProcess(self.args, -1, stdout, stderr))

    def _finish_exited(
        self, returncode: int
    ) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
        assert self._summary_path is not None
        elapsed = time.monotonic() - self.started
        written = self._summary_path.exists() and self._summary_path.stat().st_size > 0
        if not written and self.timeout is not None and elapsed >= self.timeout:
            # Exited without a session summary after the budget: the child's own
            # deadline (or an external kill) ended it, so report the timeout.
            return self._finish_timed_out()
        loaded = _read_summary(self._summary_path)
        summary = _completed_summary(returncode, elapsed, loaded)
        # Like `run_pytest`, uncaptured streams are inherited and reported as None.
        stdout, stderr = self._read_output() if self.capture_output else (None, None)
        return self._store(summary, subprocess.CompletedProcess(self.args, returncode, stdout, stderr))

    def _store(
        self, summary: dict[str, Any], proc: subprocess.CompletedProcess[str]
    ) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
        self._result = (summary, proc)
        self._cleanup_files()
        return self._result

    def _cleanup_files(self) -> None:
        for name in ("_stdout", "_stderr"):
            handle = getattr(self, name, None)
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
                setattr(self, name, None)
        path = getattr(self, "_summary_path", None)
        if path is not None:
            path.unlink(missing_ok=True)
            self._summary_path = None


def _decode_spool(handle: Any) -> str:
    if handle is None:
        return ""
    handle.flush()
    handle.seek(0)
    data = handle.read()
    text = data.decode(locale.getpreferredencoding(False), errors="replace")
    # Match subprocess text-mode universal newlines.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _new_summary_path() -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="anchor-pytest-", suffix=".json")
    os.close(fd)
    summary_path = Path(raw_path)
    summary_path.chmod(0o600)
    return summary_path


def _launch_spec(
    pytest_args: Sequence[str], summary_path: Path, python: str | None = None,
    environment: dict[str, Any] | None = None, cwd: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    if environment is not None:
        return environment_launch(
            environment, ["python", "-B", str(Path(__file__).resolve()), "-p", "no:cacheprovider", *pytest_args],
            cwd=cwd, variables={SUMMARY_ENV: str(summary_path)},
        )
    env = child_environment()
    env[SUMMARY_ENV] = str(summary_path)
    cmd = [
        python or sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "-p",
        "no:cacheprovider",
        *pytest_args,
    ]
    return env, cmd


def _read_summary(summary_path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _completed_summary(returncode: int, duration_s: float, loaded: dict[str, Any]) -> dict[str, Any]:
    summary = _empty_summary(returncode, duration_s)
    for field in COUNT_FIELDS:
        value = loaded.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            summary[field] = value
    reasons = loaded.get("skip_reasons")
    if isinstance(reasons, list):
        summary["skip_reasons"] = [
            str(item)[:MAX_SKIP_REASON_CHARS] for item in reasons[:MAX_SKIP_REASONS]
            if isinstance(item, str)
        ]
    if returncode != 0 and not any(summary[field] for field in ("failed", "errors")):
        summary["errors"] = 1
    return summary


def _empty_summary(exit_code: int, duration_s: float, *, timed_out: bool = False) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "exit_code": exit_code,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "duration_s": round(max(0.0, duration_s), 3),
        "timed_out": timed_out,
    }


class _SummaryPlugin:
    def __init__(self) -> None:
        self.counts = dict.fromkeys(COUNT_FIELDS, 0)
        self.skip_reasons: list[str] = []
        self.started = time.monotonic()

    def _record_skip(self, report: Any) -> None:
        # Bounded, de-duplicated reasons let a strict criterion name missing dependencies.
        longrepr = getattr(report, "longrepr", None)
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else longrepr
        text = str(reason or "")[:MAX_SKIP_REASON_CHARS]
        if text and text not in self.skip_reasons and len(self.skip_reasons) < MAX_SKIP_REASONS:
            self.skip_reasons.append(text)

    def pytest_runtest_logreport(self, report: Any) -> None:
        # pytest reports an expected failure as skipped and a non-strict unexpected
        # pass as passed, both marked with `wasxfail`. A strict XPASS is a failure
        # without that marker, so it stays in `failed`.
        expected_failure = hasattr(report, "wasxfail")
        if report.skipped:
            self.counts["xfailed" if expected_failure else "skipped"] += 1
            if not expected_failure:
                self._record_skip(report)
        elif report.when == "call" and report.passed and expected_failure:
            self.counts["xpassed"] += 1
        elif report.when == "call":
            self.counts["passed" if report.passed else "failed"] += 1
        elif report.failed:
            self.counts["errors"] += 1

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.counts["errors"] += 1
        elif report.skipped:
            self.counts["skipped"] += 1
            self._record_skip(report)

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        path = os.environ.get(SUMMARY_ENV)
        if not path:
            return
        summary = _empty_summary(int(exitstatus), time.monotonic() - self.started)
        summary.update(self.counts)
        summary["skip_reasons"] = list(self.skip_reasons)
        Path(path).write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")


def pytest_configure(config: Any) -> None:
    """Register the supported lifecycle-hook summary plugin."""
    config.pluginmanager.register(_SummaryPlugin(), "odibi-anchor-summary")


_CHILD_DEADLINE_STREAM: list[Any] = []


def _arm_child_deadline() -> None:
    """Exit this child after `DEADLINE_ENV` seconds without a Python thread.

    `faulthandler`'s C-level watchdog dumps tracebacks to stderr and hard-exits.
    pytest's own faulthandler plugin re-arms the same watchdog only when
    `faulthandler_timeout` is configured; then that per-test bound applies instead.
    """
    raw = os.environ.pop(DEADLINE_ENV, None)
    if not raw:
        return
    try:
        seconds = float(raw)
    except ValueError:
        return
    if seconds > 0:
        import faulthandler

        try:
            # A duplicate keeps the dump on the real stderr after pytest's fd capture.
            stream: Any = os.fdopen(os.dup(2), "w")
        except OSError:
            stream = sys.stderr
        faulthandler.dump_traceback_later(seconds, exit=True, file=stream)
        # faulthandler keeps only the fd, so the file object must stay referenced.
        _CHILD_DEADLINE_STREAM.append(stream)


def _child_main() -> int:
    """Run pytest with this exact loaded file as its structured-summary plugin."""
    child_module = sys.modules[__name__]
    sys.modules[_CANONICAL_PLUGIN_NAME] = child_module
    child_module.__name__ = _CANONICAL_PLUGIN_NAME
    _arm_child_deadline()

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
