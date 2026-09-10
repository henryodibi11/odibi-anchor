"""odibi_anchor.codebase.safe_change_context — Composable edit pipeline.

Chains guardrail → semantic_edit → touched → preflight → test_focus into a
single call. The agent makes ONE decision (what to change) instead of
sequencing 5 separate tool calls.

Pipeline steps:
  0a. Auto-map (if not yet called this session)
  0b. known_bad guardrail (blocks on high-confidence failure patterns)
  1.  semantic_edit (dry-run AST transform)
  2.  Apply to disk (if apply=True and no errors)
  2b. Register in session via touched (syntax check + session tracking)
  3.  Preflight (lint + compile check on changed file)
  4.  Test focus (identify affected test files)

File locking: Uses fcntl.flock (LOCK_EX) during apply to prevent concurrent
overwrites on the same file. Lock is held only during the write operation.

Dependencies: stdlib only (uses other odibi_anchor tools internally).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format, build_base_context
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


# Cross-platform file locking
def _lock_file(fd):
    """Acquire an exclusive non-blocking lock on *fd*."""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd):
    """Release the lock held on *fd*."""
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def safe_change_context(
    root: str | Path,
    *,
    target: str,
    action: str,
    function: str | None = None,
    verify: bool = True,
    test: bool = True,
    guardrail: bool = True,
    apply: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
    frame: Any | None = None,
    # Pass-through kwargs for semantic_edit_context
    **edit_kwargs: Any,
) -> dict[str, Any] | str:
    """Compose guardrail → edit → verify → test into a single pipeline call.

    Chains known_bad_change_context, semantic_edit_context, preflight_context,
    and test_focus_context in sequence. Returns combined results with an
    overall safety assessment.

    Args:
        root: Project root directory.
        target: Relative path to file to edit.
        action: Edit action (see semantic_edit_context).
        function: Function name to modify.
        verify: If True, run preflight_context after edit.
        test: If True, run test_focus_context to identify affected tests.
        guardrail: If True, check proposed change against known failures
            before editing. Blocks on high-confidence known-bad patterns.
        apply: If True, write changes to disk. Default False (dry-run).
        subject: Human label.
        output_format: "dict" or "markdown".
        **edit_kwargs: Additional kwargs passed to semantic_edit_context
            (param_name, param_type, new_name, etc.).

    Returns:
        Structured context dict with edit, preflight, and test_focus sub-results.
    """
    validate_output_format(output_format)

    from odibi_anchor.codebase.semantic_edit_context import semantic_edit_context

    root = Path(root).resolve()
    subject = subject or function or Path(target).stem

    # Step 0a: Auto-map if not yet called this session (fast — <200ms)
    # Ensures codebase structure is understood before any edit
    map_result = None
    try:
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS as _ss_timings
        _map_called = any(t["action"] == "map" for t in _ss_timings)
        if not _map_called:
            from odibi_anchor.codebase.codebase_map_context import codebase_map_context
            map_result = codebase_map_context(root, output_format="dict")
    except Exception as exc:
        pass  # SILENT-OK: map is advisory, not blocking for edit safety

    # Step 0b: Pre-edit guardrail
    guardrail_result = None
    _guardrail_degraded = None
    if guardrail:
        try:
            from odibi_anchor.codebase.known_bad_change_context import known_bad_change_context
            guardrail_result: dict[str, Any] | None = known_bad_change_context(  # type: ignore[assignment]
                root,
                changed_files=[target],
                action=action,
            )
        except Exception as exc:
            guardrail_result = None
            # Guardrail check failed — surface degradation so agent knows
            _guardrail_degraded = f"⚠️ Guardrail degraded: {type(exc).__name__}: {exc}"

    # If guardrail blocks, skip edit entirely
    guardrail_blocked = (
        isinstance(guardrail_result, dict)
        and guardrail_result.get("metrics", {}).get("status") == "block"
    )

    if guardrail_blocked and isinstance(guardrail_result, dict):
        # Return early with block status — don't waste an edit cycle
        block_risks = guardrail_result.get("risks", [])
        block_findings = guardrail_result.get("findings", [])
        block_actions = guardrail_result.get("suggested_next_actions", [])

        metrics = {
            "action": action,
            "target": target,
            "has_changes": False,
            "applied": False,
            "preflight_passed": False,
            "affected_test_files": 0,
            "is_safe": False,
            "guardrail_status": "block",
        }

        summary = (
            f"BLOCKED: {action} on {subject} — "
            f"guardrail detected known-bad pattern"
        )

        ctx = build_base_context(
            kind="safe_change_context",
            subject=subject,
            summary=summary,
            metrics=metrics,
            findings=block_findings,
            risks=block_risks,
            samples={},
            suggested_next_actions=block_actions,
            edit=None,
            preflight=None,
            test_focus=None,
            guardrail=guardrail_result,
            diff_preview="",
        )

        if output_format == "markdown":
            return render_safe_change_report(ctx)
        return ctx

    # Step 1: Semantic edit (always dry-run first)
    edit_result: dict[str, Any] = semantic_edit_context(  # type: ignore[assignment]
        root,
        target=target,
        action=action,
        function=function,
        apply=False,  # Always dry-run first
        **edit_kwargs,
    )

    has_edit_error = edit_result.get("transform_error") is not None
    has_changes = edit_result.get("metrics", {}).get("has_changes", False)

    # Step 1b: Post-dry-run content guardrail (static anti-pattern scan)
    # Re-runs known_bad with the actual diff so static patterns can scan code bodies
    content_guardrail_result: dict[str, Any] | None = None
    _content_guardrail_degraded = None
    if has_changes and not has_edit_error:
        diff_preview = edit_result.get("diff_preview", "")
        if diff_preview:
            try:
                from odibi_anchor.codebase.known_bad_change_context import known_bad_change_context
                content_guardrail_result = known_bad_change_context(  # type: ignore[assignment]
                    root,
                    proposed_diff=diff_preview,
                    changed_files=[target],
                    action=action,
                )
            except Exception as exc:
                content_guardrail_result = None
                _content_guardrail_degraded = f"⚠️ Content guardrail degraded: {type(exc).__name__}: {exc}"

    # Step 1c: Manifest constraint checks (forbidden_patterns + sensitive_columns)
    # Checks proposed diff against project-declared constraints from .anchor_manifest.json
    manifest_violations: list[str] = []
    sensitive_col_warnings: list[str] = []
    try:
        from odibi_anchor.codebase._manifest import load_manifest
        import re as _re_mod
        _manifest_data = load_manifest(root)
        _constraints = _manifest_data.get("constraints", {})

        if has_changes and not has_edit_error:
            _diff_text = edit_result.get("diff_preview", "")
            # Only check added lines (lines starting with "+") to avoid false positives
            _added_lines = [
                line[1:]  # Strip the leading "+"
                for line in _diff_text.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ]
            _added_content = "\n".join(_added_lines)

            # Step 1c-i: forbidden_patterns (simple substring match)
            _forbidden = _constraints.get("forbidden_patterns", [])
            for pattern in _forbidden:
                if pattern in _added_content:
                    manifest_violations.append(
                        f"Forbidden pattern '{pattern}' found in proposed code changes"
                    )

            # Step 1c-ii: sensitive_columns (regex match against string literals)
            _sensitive = _constraints.get("sensitive_columns", [])
            if _sensitive:
                # Extract string literals from added lines (single and double quoted)
                _string_literals = _re_mod.findall(
                    r"""(?:['\"]([\w.*]+)['\")])""",
                    _added_content,
                )
                # Deduplicate for efficiency
                _unique_literals = set(_string_literals)
                for col_pattern in _sensitive:
                    try:
                        _col_re = _re_mod.compile(col_pattern, _re_mod.IGNORECASE)
                    except _re_mod.error:
                        continue  # Skip invalid regex patterns
                    for literal in _unique_literals:
                        if _col_re.fullmatch(literal):
                            sensitive_col_warnings.append(
                                f"Column '{literal}' matches sensitive pattern '{col_pattern}'"
                            )
            # Step 1c-iii: read_only_catalogs (write-pattern detection)
            _ro_catalogs = _constraints.get("read_only_catalogs", [])
            if _ro_catalogs:
                _write_patterns = [
                    ".write", ".save(", "INSERT INTO", "INSERT OVERWRITE",
                    "CREATE TABLE", "CREATE OR REPLACE TABLE",
                    "MERGE INTO", "DELETE FROM", "UPDATE ",
                    ".saveAsTable(",
                ]
                for catalog in _ro_catalogs:
                    # Check if catalog name appears near a write pattern
                    if catalog in _added_content:
                        for wp in _write_patterns:
                            if wp.lower() in _added_content.lower():
                                manifest_violations.append(
                                    f"Write operation detected targeting read-only catalog '{catalog}'"
                                )
                                break  # One warning per catalog is enough

            # Step 1c-iv: max_table_rows_local (local processing warning)
            _max_local = _constraints.get("max_table_rows_local")
            if _max_local and isinstance(_max_local, int):
                _local_patterns = [".toPandas()", ".collect()", "pd.read_", "pandas.read_"]
                _found_local = [p for p in _local_patterns if p in _added_content]
                if _found_local:
                    manifest_violations.append(
                        f"Local processing pattern ({', '.join(_found_local)}) detected — "
                        f"manifest limit is {_max_local:,} rows. Ensure dataset fits in memory."
                    )

    except Exception as exc:
        manifest_violations.append(f"⚠️ Manifest check degraded: {type(exc).__name__}: {exc}")

    # Step 1c-v: Blast radius check from frame's cached codebase map
    if frame and hasattr(frame, "code_context") and frame.code_context.codebase_map:
        _rev_graph = frame.code_context.codebase_map.get("reverse_dependency_graph", {})
        _callers = _rev_graph.get(target, [])
        if len(_callers) >= 3:
            manifest_violations.append(
                f"HIGH BLAST RADIUS: {target} has {len(_callers)} dependents — "
                "run anchor('test') after applying"
            )

    # Step 2: Apply if requested and no errors (with file locking)
    touched_result = None
    if apply and has_changes and not has_edit_error:
        abs_target = root / target if not Path(target).is_absolute() else Path(target)
        try:
            abs_target.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"Target path escapes project root: {target}")
        # Snapshot baseline BEFORE applying edit (so session diffs are accurate)
        from odibi_anchor._utils._session_state import (
            _SESSION_DIFF_BASELINES,
            canonical_session_path,
        )
        session_target = canonical_session_path(target, str(root))
        if session_target not in _SESSION_DIFF_BASELINES:
            try:
                if abs_target.exists():
                    _SESSION_DIFF_BASELINES[session_target] = abs_target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        lock_path = abs_target.with_suffix(abs_target.suffix + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = open(lock_path, "w")
            _lock_file(lock_fd)
            try:
                edit_result = semantic_edit_context(  # type: ignore[assignment]
                    root,
                    target=target,
                    action=action,
                    function=function,
                    apply=True,
                    **edit_kwargs,
                )
            finally:
                _unlock_file(lock_fd)
                lock_fd.close()
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except BlockingIOError:
            # Another process holds the lock — fail safe
            edit_result["metrics"] = edit_result.get("metrics", {})
            edit_result["metrics"]["applied"] = False
            edit_result["metrics"]["lock_conflict"] = True
            edit_result["metrics"]["lock_conflict_msg"] = (
                f"LOCK CONFLICT: Another process is editing {target}. "
                f"Edit was NOT applied. Retry after the other operation completes."
            )
            has_changes = False  # Prevent downstream steps from running on stale state
        except OSError as exc:
            import errno
            if exc.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                # Windows lock conflict — same as BlockingIOError
                edit_result["metrics"] = edit_result.get("metrics", {})
                edit_result["metrics"]["applied"] = False
                edit_result["metrics"]["lock_conflict"] = True
                edit_result["metrics"]["lock_conflict_msg"] = (
                    f"LOCK CONFLICT: Another process is editing {target}. "
                    f"Edit was NOT applied. Retry after the other operation completes."
                )
                has_changes = False
            else:
                # Lock unavailable (e.g., filesystem doesn't support flock) — fall back to unlocked
                edit_result = semantic_edit_context(  # type: ignore[assignment]
                    root,
                    target=target,
                    action=action,
                    function=function,
                    apply=True,
                    **edit_kwargs,
                )

        # Step 2b: Register in session (touched) — only after successful apply
        # Uses shared _session_state singleton so gate/map see the same state
        if edit_result.get("metrics", {}).get("applied", False):
            try:
                from odibi_anchor._utils._session_state import touched as _session_touched
                touched_result = _session_touched(target, root=str(root))
            except Exception:
                touched_result = None  # SILENT-OK: touched is advisory, does not affect edit safety

    # Step 3: Preflight (only if applied or has changes to verify)
    preflight_result = None
    if verify and has_changes:
        from odibi_anchor.codebase.preflight_context import preflight_context
        preflight_result: dict[str, Any] | None = preflight_context(  # type: ignore[assignment]
            root,
            changed_files=[target],
        )

    # Step 4: Test focus (identify affected tests)
    test_focus_result = None
    if test and has_changes:
        try:
            from odibi_anchor.codebase.focus_context import test_focus_context
            test_focus_result: dict[str, Any] | None = test_focus_context(  # type: ignore[assignment]
                root,
                changed_files=[target],
                changed_functions=[function] if function else None,
                include_test_ids=True,
            )
        except Exception:
            # SILENT-OK: test_focus_context is advisory — may not be available on all projects
            test_focus_result = None

    # Overall safety assessment
    edit_safe = has_changes and not has_edit_error
    preflight_safe = (
        preflight_result is None
        or preflight_result.get("metrics", {}).get("is_safe", True)
    )
    is_safe = edit_safe and preflight_safe

    # Determine guardrail status string
    if guardrail_result is None:
        guardrail_status = "skipped"
    else:
        guardrail_status = guardrail_result.get("metrics", {}).get("status", "ok")

    fn_not_found_flag = edit_result.get("metrics", {}).get("function_not_found", False)

    # Touched evidence
    touched_syntax = None
    if touched_result:
        touched_syntax = touched_result.get("syntax_check")

    metrics = {
        "action": action,
        "target": target,
        "has_changes": has_changes,
        "applied": edit_result.get("metrics", {}).get("applied", False),
        "preflight_passed": preflight_safe,
        "affected_test_files": (
            test_focus_result.get("metrics", {}).get("affected_test_files_count", 0)
            if test_focus_result else 0
        ),
        "is_safe": is_safe,
        "function_not_found": fn_not_found_flag,
        "guardrail_status": guardrail_status,
        "touched_syntax_check": touched_syntax,
        "lock_conflict": edit_result.get("metrics", {}).get("lock_conflict", False),
        "static_patterns_matched": (
            content_guardrail_result.get("metrics", {}).get("static_patterns_matched", 0)
            if content_guardrail_result else 0
        ),
        "manifest_violations": len(manifest_violations),
        "sensitive_column_refs": len(sensitive_col_warnings),
    }

    findings = []
    # Surface guardrail degradation warnings (safety-critical)
    if _guardrail_degraded:
        findings.append(_guardrail_degraded)
    if _content_guardrail_degraded:
        findings.append(_content_guardrail_degraded)
    # Include pre-edit guardrail warnings if present
    if guardrail_status == "warn" and guardrail_result:
        for f in guardrail_result.get("findings", []):
            findings.append(f"Guardrail: {f}")
    # Include post-edit content guardrail warnings (static anti-patterns)
    if content_guardrail_result:
        cg_status = content_guardrail_result.get("metrics", {}).get("status", "ok")
        if cg_status == "warn":
            for f in content_guardrail_result.get("findings", []):
                findings.append(f"Content scan: {f}")
    # Manifest constraint violations
    if manifest_violations:
        for mv in manifest_violations:
            findings.append(f"Manifest: {mv}")
    # Sensitive column references
    if sensitive_col_warnings:
        for scw in sensitive_col_warnings:
            findings.append(f"Sensitive: {scw}")
    if has_changes:
        findings.append(f"Edit: {edit_result.get('summary', '')}")
    if preflight_result:
        findings.append(f"Preflight: {preflight_result.get('summary', '')}")
    if test_focus_result:
        findings.append(f"Tests: {test_focus_result.get('summary', '')}")
    if not has_changes:
        fn_not_found = edit_result.get("metrics", {}).get("function_not_found", False)
        if fn_not_found:
            findings.append(
                f"WARNING: Function '{function}' not found in {target}. "
                f"No edit was possible — verify the function name."
            )
        else:
            findings.append("No changes produced by the edit.")

    risks = list(edit_result.get("risks", []))
    if preflight_result:
        risks.extend(preflight_result.get("risks", []))
    # Include guardrail warnings in risks
    if guardrail_status == "warn" and guardrail_result:
        risks.extend(guardrail_result.get("risks", []))
    # Include content guardrail warnings (static anti-patterns)
    if content_guardrail_result:
        cg_risks = content_guardrail_result.get("risks", [])
        risks.extend(cg_risks)
    # Include manifest constraint violations in risks
    if manifest_violations:
        for mv in manifest_violations:
            risks.append(f"MANIFEST CONSTRAINT: {mv}")
    # Include sensitive column references in risks
    if sensitive_col_warnings:
        for scw in sensitive_col_warnings:
            risks.append(f"PII RISK: {scw}")
    # Include lock conflict warning
    lock_msg = edit_result.get("metrics", {}).get("lock_conflict_msg")
    if lock_msg:
        risks.append(lock_msg)

    suggested_actions = []

    # Scope change detection
    from odibi_anchor._utils._session_state import _SESSION_TIMINGS
    planned_actions = [t for t in _SESSION_TIMINGS if t.get("action") == "task"]
    if not planned_actions:
        suggested_actions.insert(0, 'MUST: No planning detected this session. Run anchor("task") before editing.')

    suggested_actions.append(
        "MUST: Confirm the accepted task's bounded memory selections were reviewed "
        f"for {target}; do not substitute a standalone store query."
    )

    if is_safe and not edit_result.get("metrics", {}).get("applied"):
        suggested_actions.append(
            "MUST: Review diff, then apply with safe_change_context(..., apply=True)"
        )
    if is_safe and edit_result.get("metrics", {}).get("applied"):
        if test_focus_result and test_focus_result.get("run_command"):
            suggested_actions.append(
                f"MUST: Run tests: {test_focus_result['run_command']}"
            )
    if not is_safe:
        suggested_actions.append("MUST: Fix issues before applying changes.")
    if guardrail_status == "warn":
        suggested_actions.append(
            "MUST: Keep matched candidates advisory and verify them against current evidence. "
            "Retrieval, application, evaluation, and counters do not promote; authority requires "
            "a typed verifier or separate authenticated owner requests."
        )
        suggested_actions.append(
            "MUST: After gate, use anchor('learning', 'capture|assess', ...) with real Observations or a truthful nothing_reusable_learned assessment."
        )

    # anchor() workflow hints — touched is now auto-called on apply
    if not edit_result.get("metrics", {}).get("applied", False):
        suggested_actions.append(
            "MUST: After applying, anchor('touched') will run automatically."
        )

    summary = (
        f"{'SAFE' if is_safe else 'UNSAFE'}: "
        f"{action} on {subject} — "
        f"{'FUNCTION NOT FOUND' if fn_not_found_flag else ('applied' if metrics['applied'] else 'dry-run')}, "
        f"{'preflight passed' if preflight_safe else 'preflight FAILED'}"
        f"{' (guardrail: warn)' if guardrail_status == 'warn' else ''}"
    )

    ctx = build_base_context(
        kind="safe_change_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_actions,
        edit=edit_result,
        preflight=preflight_result,
        test_focus=test_focus_result,
        guardrail=guardrail_result,
        content_guardrail=content_guardrail_result,
        diff_preview=edit_result.get("diff_preview", ""),
    )

    if output_format == "markdown":
        return render_safe_change_report(ctx)
    return ctx


def render_safe_change_report(ctx: dict[str, Any]) -> str:
    """Render safe_change_context output as markdown."""
    lines = render_header_lines(ctx, "Safe Change")

    # Overall status
    status = "SAFE" if ctx["metrics"]["is_safe"] else "UNSAFE"
    guardrail_status = ctx["metrics"].get("guardrail_status", "skipped")
    if guardrail_status == "block":
        status = "BLOCKED"
    lines.extend(["", f"**Status: {status}**", ""])

    if guardrail_status != "skipped":
        lines.append(f"Guardrail: {guardrail_status.upper()}")
        lines.append("")

    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx.get("diff_preview"):
        lines.extend(["", "## Diff", "", "```diff",
                       ctx["diff_preview"], "```"])

    lines.extend(render_bullet_section(ctx.get("findings", []), "## Pipeline Results"))
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "## Next Actions"))

    return "\n".join(lines)
