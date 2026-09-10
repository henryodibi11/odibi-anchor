"""_session_tools.py — Session-level tool functions (test runner, etc.).

Extracted from agent_init.py Phase 1 (revamp spec).
Functions that orchestrate subprocesses or session-level actions.
"""
import os as _os

from odibi_anchor._dispatcher._request_adapter import redact_message
from odibi_anchor.pytest_runner import run_pytest


def _diagnostic_output_tail(stdout, stderr):
    """Return a bounded, redacted stdout-then-stderr subprocess tail."""
    channels = []
    for channel in (stdout, stderr):
        if isinstance(channel, bytes):
            channel = channel.decode("utf-8", errors="replace")
        if channel:
            channels.append(str(channel).strip())
    output_lines = redact_message("\n".join(channels)).splitlines()
    return "\n".join(output_lines[-50:])[-8000:]


def _format_test_result(result, output_format):
    """Format test result as dict or markdown."""
    if output_format == "markdown":
        lines = [f"# Test Run: {result['subject']}\n"]
        lines.append(f"**Summary:** {result['summary']}\n")
        lines.append("## Metrics\n")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for k, v in result["metrics"].items():
            lines.append(f"| {k} | {v} |")
        if result["findings"]:
            lines.append("\n## Findings\n")
            for f in result["findings"]:
                lines.append(f"- {f}")
        if result["risks"]:
            lines.append("\n## Risks\n")
            for r in result["risks"]:
                lines.append(f"- {r}")
        if result.get("suggested_next_actions"):
            lines.append("\n## Next Actions\n")
            for a in result["suggested_next_actions"]:
                lines.append(f"- {a}")
        return "\n".join(lines)
    return result


def _test_run(root, failure_pattern_fn=None, *args, **kwargs):
    """Run pytest and return structured results.

    Args:
        root: Project root directory (cwd for pytest).
        failure_pattern_fn: Optional callable for failure pattern analysis.
            Signature: fn(text, root=root, output_format="dict") -> dict.
        target: Optional file/pattern for focused run.
        mark: Optional marker filter (e.g. 'fast', 'slow').
        timeout: Per-test timeout in seconds (default: 30).
        verbose: If True, include full output in samples.
        output_format: 'dict' or 'markdown'.

    Returns:
        Contract-compliant dict: kind='test_run', metrics={passed, failed, errors, duration_s}, ...
    """
    supported = {"target", "mark", "timeout", "verbose", "output_format"}
    unsupported = sorted(set(kwargs) - supported)
    if unsupported:
        raise ValueError(
            f"Unsupported anchor('test') argument(s): {', '.join(unsupported)}. "
            f"Supported: {', '.join(sorted(supported))}."
        )
    target = kwargs.pop("target", args[0] if args else None)
    # Normalize space-separated string targets into a list
    if isinstance(target, str) and " " in target.strip():
        target = target.split()
    mark = kwargs.pop("mark", None)
    timeout = kwargs.pop("timeout", 30)
    verbose = kwargs.pop("verbose", False)
    output_format = kwargs.pop("output_format", "dict")

    # Build pytest arguments. The shared runner supplies sys.executable, cache
    # isolation, stdin isolation, hermetic Git settings, and structured counts.
    pytest_args = ["--no-header", "-q", "--tb=short", "--color=no"]

    # Add timeout only if pytest-timeout is installed
    try:
        import pytest_timeout  # noqa: F401
        pytest_args.append(f"--timeout={timeout}")
    except ImportError:
        pass  # pytest-timeout not available — skip timeout flag

    if target:
        if isinstance(target, (list, tuple)):
            pytest_args.extend(str(t) for t in target)
        else:
            pytest_args.append(str(target))
    else:
        tests_dir = _os.path.join(root, "tests")
        if _os.path.isdir(tests_dir):
            pytest_args.append("tests/")
        else:
            pytest_args.append(".")

    if mark:
        pytest_args.extend(["-m", mark])

    summary_data, proc = run_pytest(pytest_args, cwd=root, timeout=600, capture_output=True)
    cmd = proc.args
    if summary_data["timed_out"]:
        samples = {
            "command": " ".join(cmd),
            "output_tail": _diagnostic_output_tail(proc.stdout, proc.stderr),
        }
        return _format_test_result({
            "kind": "test_run", "subject": root,
            "summary": "TIMEOUT: Test suite exceeded 10m total timeout",
            "metrics": {"passed": 0, "failed": 0, "errors": 1, "duration_s": 600.0,
                        "exit_code": -1, "timed_out": True},
            "findings": ["Test suite timed out after 600s"],
            "risks": ["Suite may have hanging tests — investigate with --timeout flag"],
            "samples": samples,
            "suggested_next_actions": [
                "MUST: Identify hanging test(s) — run with -x --timeout=10",
                "MUST: Mock network/API calls in slow tests",
            ],
        }, output_format)

    # Counts come exclusively from supported pytest lifecycle hooks.
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    passed = summary_data["passed"]
    failed = summary_data["failed"]
    errors = summary_data["errors"]
    duration = summary_data["duration_s"]

    # Build findings
    findings = []
    if proc.returncode == 0:
        findings.append(f"All {passed} test(s) passed in {duration:.1f}s")
    else:
        findings.append(f"{failed} failed, {errors} error(s) out of {passed + failed + errors} tests")
        # Extract failure names
        for line in stdout.split("\n"):
            if line.startswith("FAILED "):
                findings.append(line.strip())

    # On failure, run failure_pattern matching
    risks = []
    if proc.returncode != 0 and stderr.strip() and failure_pattern_fn:
        try:
            fp_result = failure_pattern_fn(stderr[:2000], root=root, output_format="dict")
            if isinstance(fp_result, dict) and fp_result.get("findings"):
                risks.extend(fp_result["findings"][:3])
        except Exception as _exc:
            from odibi_anchor._utils._session_state import record_degraded
            record_degraded("test_failure_pattern_analysis", _exc)

    # Build samples
    samples = {"command": " ".join(cmd), "exit_code": proc.returncode}
    if verbose or proc.returncode != 0:
        # Pytest startup/plugin failures can write only to stderr. Preserve both
        # channels while keeping the diagnostic bounded and credential-redacted.
        samples["output_tail"] = _diagnostic_output_tail(stdout, stderr)

    result = {
        "kind": "test_run",
        "subject": (", ".join(target) if isinstance(target, (list, tuple)) else target) or "full_suite",
        "summary": f"{'PASS' if proc.returncode == 0 else 'FAIL'}: {passed} passed, {failed} failed, {errors} errors ({duration:.1f}s)",
        "metrics": {
            "passed": passed, "failed": failed, "errors": errors,
            "skipped": summary_data["skipped"],
            "duration_s": duration, "exit_code": proc.returncode,
            "timed_out": False,
        },
        "findings": findings,
        "risks": risks,
        "samples": samples,
        "suggested_next_actions": (
            ["All tests pass — safe to proceed.", "MUST: Run anchor('gate') to verify session compliance."]
            if proc.returncode == 0
            else ["MUST: Fix failing tests before proceeding.",
                  f"MUST: Run anchor('test', target='{findings[1].split(chr(58)+chr(58))[0].replace(chr(70)+chr(65)+chr(73)+chr(76)+chr(69)+chr(68)+chr(32), chr(32)).strip() if len(findings) > 1 else 'tests/'}') for focused re-run"]
        ),
    }
    return _format_test_result(result, output_format)


def _auto_scope_tests(kwargs, *, session_files_changed, test_focus_fn, root):
    """Auto-scope test target from session changed files when no target provided.

    Uses focus_context's affected test discovery to find only the tests
    that reference changed modules. An explicit non-Python scope or a scope
    with no discoverable tests fails before spawning pytest rather than silently
    widening to the full suite.
    """
    supported = {"target", "changed_files", "mark", "timeout", "verbose", "output_format"}
    unsupported = sorted(set(kwargs) - supported)
    if unsupported:
        raise ValueError(
            f"Unsupported anchor('test') argument(s): {', '.join(unsupported)}. "
            f"Supported: {', '.join(sorted(supported))}."
        )

    if "target" in kwargs:
        cleaned = dict(kwargs)
        cleaned.pop("changed_files", None)
        return cleaned

    explicit_changed = kwargs.get("changed_files")
    if explicit_changed is not None and not isinstance(explicit_changed, (list, tuple, set)):
        raise TypeError("changed_files must be a list, tuple, or set of paths")
    changed_scope = list(explicit_changed) if explicit_changed is not None else list(session_files_changed)
    if not changed_scope:
        cleaned = dict(kwargs)
        cleaned.pop("changed_files", None)
        return cleaned  # No scope means an intentional full-suite invocation.

    py_changed = [str(f) for f in changed_scope if str(f).endswith(".py")]
    if not py_changed:
        raise ValueError(
            "No Python tests are applicable to the non-Python-only change scope. "
            "Use anchor('review') and anchor('gate') for documentation work."
        )

    try:
        ctx = test_focus_fn(root, changed_files=py_changed, output_format="dict")
        affected = ctx.get("affected_test_files", [])
        if affected:
            targets = [a["path"] for a in affected]
            cleaned = {key: value for key, value in kwargs.items() if key != "changed_files"}
            return {**cleaned, "target": targets}
    except Exception as exc:
        raise RuntimeError(
            f"Test auto-scope failed ({type(exc).__name__}: {exc}); no tests were run. "
            "Pass target=... for a focused run or call anchor('test') with an empty change ledger "
            "for an intentional full-suite run."
        ) from exc
    raise ValueError(
        "No applicable tests were discovered for the Python change scope; no tests were run. "
        "Pass target=... explicitly if broader verification is intended."
    )
