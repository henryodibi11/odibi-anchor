"""odibi_anchor.codebase.preflight_context — Pre-commit verification.

Runs pyright (type checker) and ruff (linter) on changed files and returns
structured diagnostics. Provides 2-second feedback vs 11-minute test suites.

Dependencies: subprocess (stdlib). Optional: pyright (CLI), ruff (CLI).
"""

from __future__ import annotations

import ast as ast_mod
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from odibi_anchor._utils.ast_utils import ast_cache_invalidate
from odibi_anchor._utils.contract import build_base_context, validate_output_format
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
)

_DIAGNOSTIC_DETAIL_CAP = 100


def _error_fingerprint(diag: dict) -> str:
    """Stable fingerprint for a diagnostic — survives line number shifts.

    Uses source + rule + basename only. Message is excluded because pyright
    can reorder union type members between runs (e.g., ``str | dict`` vs
    ``dict | str``), causing identical errors to get different fingerprints.
    """
    basename = Path(diag.get("file", "")).name
    return f"{diag.get('source', '')}:{diag.get('rule', '')}:{basename}"


def attribute_diagnostic(
    diagnostic: dict[str, Any],
    root: str | Path,
    *,
    changed_paths: set[str],
    ranges_by_path: dict[str, list[tuple[int, int, str]]],
    created_paths: set[str],
) -> str:
    """Classify one diagnostic using the repository's shared diff-first semantics."""
    root = Path(root).resolve()
    path = Path(str(diagnostic.get("file", "")))
    try:
        candidate = path if path.is_absolute() else root / path
        relative = candidate.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return "uncertain"
    if relative in created_paths:
        return "introduced"
    line = diagnostic.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return "uncertain"
    file_ranges = ranges_by_path.get(relative)
    if file_ranges and all(kind == "deleted" for _start, _end, kind in file_ranges):
        return "pre_existing"
    if relative in changed_paths and not file_ranges:
        return "uncertain"
    if not file_ranges:
        return "pre_existing"
    if any(start <= line <= end for start, end, kind in file_ranges if kind != "deleted"):
        return "introduced"
    return "pre_existing"


def preflight_context(
    root: str | Path,
    *,
    changed_files: list[str] | None = None,
    check_types: bool = False,  # Disabled: too many false positives on workspace files
    check_lint: bool = True,
    check_syntax: bool = True,
    subject: str | None = None,
    output_format: str = "dict",
    baseline: set[str] | None = None,
    changed_line_ranges: tuple[Any, ...] | None = None,
    created_paths: tuple[str, ...] = (),
) -> dict[str, Any] | str:
    """Run pre-commit checks on changed files and return diagnostics.

    Executes available checkers (pyright, ruff, syntax parse) on the
    specified files and returns structured results. Checks are skipped
    gracefully if the tools are not installed.

    Args:
        root: Project root directory.
        changed_files: List of changed file paths (relative to root).
            If None, checks all Python files (slower).
        check_types: If True, run pyright type checker (if available).
        check_lint: If True, run ruff linter (if available).
        check_syntax: If True, verify files parse without SyntaxError.
        subject: Human label. Defaults to directory name.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    subject = subject or root.name
    explicit_scope = changed_files is not None

    # None means repository-wide scope. An explicit empty or non-Python scope
    # intentionally has no applicable files and must never fall back to the root.
    file_paths = (
        [root / f for f in changed_files if str(f).lower().endswith(".py")]
        if explicit_scope else _find_py_files(root)
    )

    # Invalidate cache for changed files so downstream tools see fresh content
    for fp in file_paths:
        ast_cache_invalidate(fp)

    diagnostics: list[dict[str, Any]] = []
    tools_available: dict[str, bool] = {}

    # Syntax check (always available — stdlib)
    if check_syntax and file_paths:
        syntax_diags = _check_syntax(file_paths)
        diagnostics.extend(syntax_diags)

    # Type check with pyright
    if check_types and file_paths:
        pyright_path = shutil.which("pyright")
        tools_available["pyright"] = pyright_path is not None
        if pyright_path:
            type_diags = _run_pyright(root, file_paths)
            diagnostics.extend(type_diags)

    # Lint with ruff
    if check_lint and file_paths:
        ruff_path = shutil.which("ruff")
        tools_available["ruff"] = ruff_path is not None
        if ruff_path:
            lint_diags = _run_ruff(root, file_paths)
            diagnostics.extend(lint_diags)

    # Manifest: required_gates check
    # Verifies the session has run all gates declared in .anchor_manifest.json
    _missing_gates: list[str] = []
    try:
        from odibi_anchor.codebase._manifest import load_manifest
        _manifest_data = load_manifest(root)
        _required = _manifest_data.get("constraints", {}).get("required_gates", [])
        if _required:
            from odibi_anchor._utils._session_state import _SESSION_TIMINGS
            _run_actions = {t.get("action") for t in _SESSION_TIMINGS}
            for gate_name in _required:
                # Map gate names to their action equivalents
                _action_name = gate_name.replace("_context", "")
                if _action_name not in _run_actions and gate_name not in _run_actions:
                    _missing_gates.append(gate_name)
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("preflight_manifest_gate", _exc)

    # Compute metrics
    errors = [d for d in diagnostics if d["severity"] == "error"]
    warnings = [d for d in diagnostics if d["severity"] == "warning"]

    is_safe = len(errors) == 0

    metrics = {
        "files_checked": len(file_paths),
        "scope": "explicit" if explicit_scope else "repository",
        "total_diagnostics": len(diagnostics),
        "errors": len(errors),
        "warnings": len(warnings),
        "type_errors": len([d for d in errors if d["source"] == "pyright"]),
        "lint_errors": len([d for d in errors if d["source"] == "ruff"]),
        "syntax_errors": len([d for d in errors if d["source"] == "syntax"]),
        "is_safe": is_safe,
        "tools_available": tools_available,
        "missing_required_gates": _missing_gates,
    }

    # Baseline filtering: only NEW errors block. Raw metrics and fingerprints
    # above remain based on the complete checker output.
    findings_baseline_note = ""
    baseline_errors: list[dict[str, Any]] = []
    new_errors = list(errors)
    if changed_line_ranges is not None:
        ranges_by_path: dict[str, list[tuple[int, int, str]]] = {}
        for item in changed_line_ranges:
            ranges_by_path.setdefault(item.path, []).append((item.start, item.end, item.kind))
        changed_paths = {str(path) for path in (changed_files or ())}
        created = set(created_paths)
        for error in errors:
            error["attribution"] = attribute_diagnostic(
                error,
                root,
                changed_paths=changed_paths,
                ranges_by_path=ranges_by_path,
                created_paths=created,
            )
        baseline_errors = [e for e in errors if e["attribution"] == "pre_existing"]
        new_errors = [e for e in errors if e["attribution"] != "pre_existing"]
    elif baseline is not None:
        new_errors = [e for e in errors if _error_fingerprint(e) not in baseline]
        baseline_errors = [e for e in errors if _error_fingerprint(e) in baseline]
    if changed_line_ranges is not None or baseline is not None:
        is_safe = len(new_errors) == 0
        metrics["is_safe"] = is_safe
        metrics["baseline_errors"] = len(baseline_errors)
        metrics["new_errors"] = len(new_errors)
        if baseline_errors:
            findings_baseline_note = (
                f"{len(baseline_errors)} pre-existing error(s) in baseline (not blocking)."
            )
        errors = new_errors  # Only new errors are blocking
    else:
        # First run — capture baseline
        current_baseline = {_error_fingerprint(e) for e in errors}
        # Store as sorted list for JSON serialization; dispatcher converts back to set
        metrics["_captured_baseline"] = sorted(current_baseline)

    # Keep responses compact without weakening raw evidence. New errors are
    # always first and baseline-matched errors are represented only by counts.
    non_baseline_warnings = [
        warning for warning in warnings
        if baseline is None or _error_fingerprint(warning) not in baseline
    ]
    returned_diagnostics = (new_errors + non_baseline_warnings)[:_DIAGNOSTIC_DETAIL_CAP]
    metrics["diagnostics_returned"] = len(returned_diagnostics)
    metrics["diagnostics_omitted_baseline"] = len(baseline_errors)
    metrics["diagnostics_truncated"] = max(
        0, len(new_errors) + len(non_baseline_warnings) - len(returned_diagnostics),
    )

    findings = []
    if baseline is not None and baseline_errors:
        findings.append(findings_baseline_note)
    if not tools_available.get("pyright") and check_types and file_paths:
        findings.append("pyright not found — type checking skipped. Install: npm install -g pyright")
    if not tools_available.get("ruff") and check_lint and file_paths:
        findings.append("ruff not found — linting skipped. Install: pip install ruff")
    if not file_paths and explicit_scope:
        findings.append("No applicable Python files in the explicit scope; Python checks skipped.")
    if errors:
        findings.append(f"{len(errors)} error(s) found — fix before proceeding.")
        for e in errors[:5]:
            findings.append(f"  {e['file']}:{e['line']} — {e['message']}")
    elif diagnostics:
        findings.append(f"No errors. {len(warnings)} warning(s).")
    else:
        findings.append("All checks passed — no issues found.")
    # Manifest required_gates findings
    if _missing_gates:
        findings.append(
            f"Manifest: {len(_missing_gates)} required gate(s) not yet run this session: "
            f"{', '.join(_missing_gates)}"
        )

    risks = []
    if not is_safe:
        risks.append(f"{len(errors)} error(s) — changes will likely break at runtime.")
    if file_paths and not any(tools_available.values()) and (check_types or check_lint):
        risks.append("No external checkers available — only syntax was verified.")
    if _missing_gates:
        risks.append(
            f"MANIFEST: Required gate(s) missing from session: {', '.join(_missing_gates)}. "
            f"Run these before anchor('gate')."
        )

    suggested_actions = []
    if errors:
        suggested_actions.append("MUST: Fix all errors before proceeding.")
        suggested_actions.append("MUST: Re-run anchor('preflight', changed_files=[...]) after fixes.")
        suggested_actions.append(
            "RECOVERY: Fix syntax/lint errors above, then re-run anchor('preflight')."
        )
    elif is_safe:
        if file_paths:
            suggested_actions.append("MUST: Run anchor('test', changed_files=[...]) to identify affected tests.")
        else:
            suggested_actions.append("SHOULD: Review the scoped artifacts before delivery.")
        suggested_actions.append(
            "MUST: Run anchor('gate', actions_taken=[...]) to close workflow obligations."
        )

    summary = (
        f"{'SAFE' if is_safe else 'UNSAFE'}: "
        f"{len(errors)} error(s), {len(warnings)} warning(s) "
        f"across {len(file_paths)} file(s)"
    )

    ctx = build_base_context(
        kind="preflight_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_actions,
        diagnostics=returned_diagnostics,
        passed=is_safe,
    )

    if output_format == "markdown":
        return render_preflight_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------


def _check_syntax(files: list[Path]) -> list[dict[str, Any]]:
    """Check Python files for syntax errors using stdlib ast."""
    diagnostics = []
    for filepath in files:
        if filepath.suffix != ".py" or not filepath.exists():
            continue
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            ast_mod.parse(source)
        except SyntaxError as exc:
            diagnostics.append({
                "file": str(filepath),
                "line": exc.lineno or 0,
                "column": exc.offset or 0,
                "message": str(exc.msg) if hasattr(exc, 'msg') else str(exc),
                "severity": "error",
                "source": "syntax",
                "rule": "SyntaxError",
            })
    return diagnostics


def _run_pyright(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    """Run pyright and parse JSON output."""
    cmd = ["pyright", "--outputjson"]
    if files:
        cmd.extend(str(f) for f in files)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=str(root),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    diagnostics = []
    try:
        data = json.loads(result.stdout)
        for diag in data.get("generalDiagnostics", []):
            severity = diag.get("severity", "error")
            if severity == "information":
                severity = "info"
            diagnostics.append({
                "file": diag.get("file", ""),
                "line": diag.get("range", {}).get("start", {}).get("line", 0) + 1,
                "column": diag.get("range", {}).get("start", {}).get("character", 0) + 1,
                "message": diag.get("message", ""),
                "severity": severity,
                "source": "pyright",
                "rule": diag.get("rule", ""),
            })
    except (json.JSONDecodeError, KeyError):
        pass

    return diagnostics


def _run_ruff(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    """Run ruff and parse JSON output."""
    cmd = ["ruff", "check", "--output-format", "json"]
    if files:
        cmd.extend(str(f) for f in files)
    else:
        cmd.append(str(root))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=str(root),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    diagnostics = []
    try:
        data = json.loads(result.stdout)
        for diag in data:
            severity = "warning" if diag.get("fix") else "error"
            diagnostics.append({
                "file": diag.get("filename", ""),
                "line": diag.get("location", {}).get("row", 0),
                "column": diag.get("location", {}).get("column", 0),
                "message": diag.get("message", ""),
                "severity": severity,
                "source": "ruff",
                "rule": diag.get("code", ""),
            })
    except (json.JSONDecodeError, KeyError):
        pass

    return diagnostics


def _find_py_files(root: Path) -> list[Path]:
    """Find all .py files under root (excluding common exclusions)."""
    py_files = []
    exclude_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox"}
    for path in root.rglob("*.py"):
        if not any(part in exclude_dirs for part in path.parts):
            py_files.append(path)
    return py_files


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_preflight_report(ctx: dict[str, Any]) -> str:
    """Render preflight_context output as markdown."""
    lines = render_header_lines(ctx, "Preflight Check")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    diagnostics = ctx.get("diagnostics", [])
    errors = [d for d in diagnostics if d["severity"] == "error"]
    warnings = [d for d in diagnostics if d["severity"] == "warning"]

    if errors:
        lines.extend(["", "## Errors", ""])
        for d in errors[:20]:
            lines.append(f"- `{d['file']}:{d['line']}` [{d['source']}] {d['message']}")

    if warnings:
        lines.extend(["", "## Warnings", ""])
        for d in warnings[:10]:
            lines.append(f"- `{d['file']}:{d['line']}` [{d['source']}] {d['message']}")

    if not errors and not warnings:
        lines.extend(["", "## All Clear", "", "No issues found."])

    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "## Next Actions"))

    return "\n".join(lines)
