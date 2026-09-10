"""odibi_anchor.codebase.test_focus_context — Targeted test discovery.

Maps changed functions/files to their affected test files using AST analysis.
Reports coverage gaps (source functions with no test references) and suggests
test cases based on function signatures and patterns.

Eliminates the "run everything" bottleneck by narrowing the test surface to
only what's affected by a code change.

Usage:
    from odibi_anchor.codebase import test_focus_context

    ctx = test_focus_context(
        "/path/to/project",
        changed_files=["src/mylib/transform.py"],
    )
    print(ctx["run_command"])
    # "pytest tests/test_transform.py -q"

Dependencies: stdlib only (ast, os, pathlib, re).
"""

from __future__ import annotations


import ast
import os
import re
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section
from odibi_anchor._utils.ast_utils import walk_py_files, parse_file_safe, parse_source_safe, read_source_cached


def test_focus_context(
    root: str | Path,
    *,
    changed_files: list[str] | None = None,
    changed_functions: list[str] | None = None,
    subject: str | None = None,
    test_dir: str = "tests",
    src_dir: str = "src",
    include_test_ids: bool = False,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Map changed functions/files to affected test files.

    Performs AST-based analysis to identify which tests are affected by
    source code changes, reports coverage gaps, and suggests test cases.

    Args:
        root: Project root directory.
        changed_files: List of changed source file paths (relative to root).
        changed_functions: List of changed function names (searched across all
            source files if no changed_files provided).
        subject: Human label. Defaults to directory name.
        test_dir: Directory name containing tests (relative to root).
        src_dir: Directory name containing source code (relative to root).
        include_test_ids: If True, produce precise test IDs (file::test_name)
            by filtering test functions that reference changed functions.
            Defaults to False for backward compatibility.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).

    Example:
        >>> ctx = test_focus_context("/path/to/project",
        ...     changed_files=["src/mylib/utils.py"])
        >>> ctx["affected_test_files"]
        [{"path": "tests/test_utils.py", "reason": "imports utils"}]
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")

    subject = subject or root.name
    changed_files = changed_files or []
    changed_functions = changed_functions or []

    # Resolve paths
    test_root = root / test_dir
    src_root = root / src_dir

    # Collect all test files
    test_files = _collect_test_files(test_root) if test_root.is_dir() else []

    # Collect all source files
    source_files = _collect_source_files(src_root) if src_root.is_dir() else []

    # Parse source files to get function/constant definitions
    source_defs = _parse_source_definitions(root, source_files)

    # Parse test files to get imports, references, and test function names
    test_refs = _parse_test_references(root, test_files)

    # Resolve changed functions from changed files
    all_changed_functions = list(changed_functions)
    changed_modules: list[str] = []
    for cf in changed_files:
        cf_path = root / cf
        if cf_path.exists():
            module_name = _path_to_module_name(cf, src_dir)
            changed_modules.append(module_name)
            # Extract all functions defined in the changed file
            for src_path, defs in source_defs.items():
                if src_path == cf:
                    all_changed_functions.extend(defs["functions"])
                    all_changed_functions.extend(defs["constants"])

    # Deduplicate
    all_changed_functions = list(dict.fromkeys(all_changed_functions))

    # Find affected test files
    affected = _find_affected_tests(
        root, test_refs, all_changed_functions, changed_modules, changed_files
    )

    # Build precise test IDs if requested
    targeted_test_ids: list[str] = []
    if include_test_ids and affected:
        for af in affected:
            test_path = af["path"]
            test_info = test_refs.get(test_path, {})
            test_funcs = test_info.get("test_functions", [])
            if test_funcs:
                # Filter to test functions that reference the changed items
                relevant_tests = _filter_relevant_tests(
                    root, test_path, test_funcs, all_changed_functions
                )
                for func_name in relevant_tests:
                    targeted_test_ids.append(f"{test_path}::{func_name}")
            else:
                # Can't determine specific tests — include whole file
                targeted_test_ids.append(test_path)

    # Find coverage gaps
    coverage_gaps = _find_coverage_gaps(source_defs, test_refs, changed_files, src_dir)

    # Count test functions in affected files for time estimate
    affected_test_count = sum(
        len(test_refs.get(a["path"], {}).get("test_functions", []))
        for a in affected
    )
    estimated_run_seconds = round(affected_test_count * 0.3, 1)

    # Generate suggested test cases
    suggested_cases = _suggest_test_cases(all_changed_functions, source_defs, changed_files)

    # Build run command
    if affected:
        affected_paths = " ".join(a["path"] for a in affected)
        run_command = f"pytest {affected_paths} -q"
    else:
        run_command = f"pytest {test_dir}/ -q"

    # Build targeted run command
    targeted_run_command = ""
    if targeted_test_ids:
        targeted_run_command = f"pytest {' '.join(targeted_test_ids)} --no-header -q"

    # Build findings
    findings = []
    if all_changed_functions:
        findings.append(
            f"{len(all_changed_functions)} changed function(s)/constant(s) identified."
        )
    if affected:
        findings.append(
            f"{len(affected)} test file(s) affected \u2014 estimated {estimated_run_seconds}s "
            f"vs full suite."
        )
    if coverage_gaps:
        findings.append(
            f"{len(coverage_gaps)} coverage gap(s) found \u2014 functions with no test references."
        )
    if not findings:
        findings.append("No changes specified \u2014 full test scan recommended.")

    # Risks
    risks = []
    if coverage_gaps:
        risks.append(
            f"{len(coverage_gaps)} function(s) have no test coverage \u2014 "
            f"changes could break silently."
        )
    if not affected and all_changed_functions:
        risks.append(
            "No test files reference the changed functions \u2014 possible dead code or "
            "integration-only coverage."
        )

    # Suggested actions (MUST/SHOULD prefixed for obligation system)
    suggested_actions = []
    if targeted_test_ids:
        suggested_actions.insert(0,
            f"MUST: Run precise: pytest {' '.join(targeted_test_ids[:10])} --no-header -q"
        )
    if affected:
        suggested_actions.append(f"MUST: Run targeted: {run_command}")
    if coverage_gaps:
        suggested_actions.append(
            f"MUST: Write tests for {len(coverage_gaps)} uncovered function(s)."
        )
    if not affected and all_changed_functions:
        suggested_actions.append("MUST: Consider adding unit tests for changed functions.")
    suggested_actions.append("MUST: After targeted pass, run full suite to confirm no regressions.")

    # anchor() workflow hints
    suggested_actions.append(
        "MUST: Run anchor('gate', actions_taken=[...]) after tests pass to close obligations."
    )
    if coverage_gaps:
        suggested_actions.append(
            "MUST: Run anchor('convention', action='new_function', ...) before writing new test functions."
        )

    # Obligation tracking
    obligations_created = []
    if affected:
        obligations_created.append({
            "tool": "pytest",
            "reason": "Run the test command above",
            "priority": "MUST",
            "suggested_call": run_command,
        })
    obligations_paid = ["test_focus_context"]

    # Metrics
    metrics = {
        "changed_functions_count": len(all_changed_functions),
        "affected_test_files_count": len(affected),
        "coverage_gaps_count": len(coverage_gaps),
        "estimated_run_seconds": estimated_run_seconds,
        "test_functions_in_scope": affected_test_count,
        "targeted_test_ids_count": len(targeted_test_ids),
    }

    ctx: dict[str, Any] = {
        "kind": "test_focus_context",
        "subject": subject,
        "summary": (
            f"{len(all_changed_functions)} changed function(s) \u2192 "
            f"{len(affected)} test file(s) affected, "
            f"{len(coverage_gaps)} coverage gap(s)"
        ),
        "metrics": metrics,
        "findings": findings,
        "risks": risks,
        "affected_test_files": affected,
        "coverage_gaps": coverage_gaps,
        "run_command": run_command,
        "targeted_test_ids": targeted_test_ids,
        "targeted_run_command": targeted_run_command,
        "suggested_test_cases": suggested_cases,
        "samples": {},
        "suggested_next_actions": suggested_actions,
        "obligations_created": obligations_created,
        "obligations_paid": obligations_paid,
    }

    if output_format == "markdown":
        return render_test_focus_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_test_focus_report(ctx: dict[str, Any]) -> str:
    """Render test_focus_context output as markdown."""
    lines = render_header_lines(ctx, "Test Focus")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx["affected_test_files"]:
        lines.extend(["", "## Affected Test Files", ""])
        for af in ctx["affected_test_files"]:
            lines.append(f"- `{af['path']}` \u2014 {af['reason']}")

    # Targeted Test IDs section
    targeted = ctx.get("targeted_test_ids", [])
    if targeted:
        lines.extend(["", "## Targeted Test IDs", ""])
        for tid in targeted[:20]:  # Cap display
            lines.append(f"- `{tid}`")
        if len(targeted) > 20:
            lines.append(f"- ... and {len(targeted) - 20} more")
        if ctx.get("targeted_run_command"):
            lines.extend(["", "```bash", ctx["targeted_run_command"], "```"])

    if ctx["coverage_gaps"]:
        lines.extend(["", "## Coverage Gaps", ""])
        for gap in ctx["coverage_gaps"]:
            lines.append(f"- `{gap['function']}` in `{gap['file']}` \u2014 {gap['reason']}")

    if ctx["run_command"]:
        lines.extend(["", "## Run Command", "", f"```bash\n{ctx['run_command']}\n```"])

    if ctx["suggested_test_cases"]:
        lines.extend(["", "## Suggested Test Cases", ""])
        for tc in ctx["suggested_test_cases"]:
            lines.append(f"- `{tc}`")

    lines.extend(render_bullet_section(ctx["findings"], "## Findings"))
    lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
    lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Suggested Actions"))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal: File Collection
# ---------------------------------------------------------------------------


def _collect_test_files(test_root: Path) -> list[Path]:
    """Collect all test_*.py / *_test.py files recursively."""
    all_py = walk_py_files(test_root)
    return [f for f in all_py if f.name.startswith("test_") or f.name.endswith("_test.py")]


def _collect_source_files(src_root: Path) -> list[Path]:
    """Collect all .py source files (excluding __pycache__, tests)."""
    return walk_py_files(src_root)


# ---------------------------------------------------------------------------
# Internal: AST Parsing
# ---------------------------------------------------------------------------


def _parse_source_definitions(
    root: Path, source_files: list[Path]
) -> dict[str, dict[str, list[str]]]:
    """Parse source files and extract function/constant definitions.

    Returns {relative_path: {"functions": [...], "constants": [...]}}.
    """
    result = {}
    for fpath in source_files:
        rel = str(fpath.relative_to(root)).replace("\\", "/")
        tree = parse_file_safe(fpath)
        if tree is None:
            continue

        functions = []
        constants = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                functions.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        constants.append(target.id)
                    elif isinstance(target, ast.Name) and target.id.startswith("_") and target.id[1:].isupper():
                        # _UPPER_CASE private constants
                        constants.append(target.id)

        result[rel] = {"functions": functions, "constants": constants}
    return result


def _parse_test_references(
    root: Path, test_files: list[Path]
) -> dict[str, dict[str, Any]]:
    """Parse test files and extract imports, function calls, and string references.

    Returns {relative_path: {"imports": [...], "references": set(...), "test_functions": [...]}}.
    """
    result = {}
    for fpath in test_files:
        rel = str(fpath.relative_to(root)).replace("\\", "/")
        source = read_source_cached(fpath)
        if source is None:
            continue
        tree = parse_file_safe(fpath)
        if tree is None:
            continue

        imports: list[str] = []
        references: set[str] = set()
        test_functions: list[str] = []

        for node in ast.walk(tree):
            # Collect imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.append(module)
                for alias in node.names:
                    references.add(alias.name)
                    imports.append(f"{module}.{alias.name}")
            # Collect function calls (Name references)
            elif isinstance(node, ast.Name):
                references.add(node.id)
            # Collect string literals (e.g., in parametrize, patch targets)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Only track identifiers (word-like strings)
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", node.value):
                    references.add(node.value)
                # Also extract segments from dotted paths (e.g., patch() targets)
                elif "." in node.value and all(
                    re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", seg)
                    for seg in node.value.split(".")
                ):
                    for seg in node.value.split("."):
                        references.add(seg)
            # Collect test function names
            elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                test_functions.append(node.name)

        result[rel] = {
            "imports": imports,
            "references": references,
            "test_functions": test_functions,
        }
    return result


# ---------------------------------------------------------------------------
# Internal: Matching Logic
# ---------------------------------------------------------------------------


def _path_to_module_name(file_path: str, src_dir: str) -> str:
    """Convert a relative file path to a dotted module name.

    E.g., "src/odibi_anchor/codebase/foo.py" \u2192 "odibi_anchor.codebase.foo"
    """
    # Strip src_dir prefix if present
    path = file_path
    if path.startswith(src_dir + "/") or path.startswith(src_dir + os.sep):
        path = path[len(src_dir) + 1:]
    # Strip .py extension
    if path.endswith(".py"):
        path = path[:-3]
    # Convert separators to dots
    module = path.replace(os.sep, ".").replace("/", ".")
    # Strip __init__ suffix
    if module.endswith(".__init__"):
        module = module[:-9]
    return module


def _find_affected_tests(
    root: Path,
    test_refs: dict[str, dict[str, Any]],
    changed_functions: list[str],
    changed_modules: list[str],
    changed_files: list[str],
) -> list[dict[str, str]]:
    """Find test files that reference any of the changed functions or modules."""
    affected = []
    seen_paths: set[str] = set()

    for test_path, refs in test_refs.items():
        if test_path in seen_paths:
            continue

        reason_parts: list[str] = []

        # Check if test imports a changed module (exact or sub-import match)
        for module in changed_modules:
            for imp in refs["imports"]:
                # Match: exact, import starts with module (sub-import), or module starts with import (parent package)
                if imp == module or imp.startswith(module + "."):
                    reason_parts.append(f"imports {module.split('.')[-1]}")
                    break

        # Check if test references changed functions
        matched_funcs = []
        for func in changed_functions:
            if func in refs["references"]:
                matched_funcs.append(func)

        if matched_funcs:
            if len(matched_funcs) <= 3:
                reason_parts.append(f"references {', '.join(matched_funcs)}")
            else:
                reason_parts.append(f"references {len(matched_funcs)} changed functions")

        # Check filename-based matching (test_foo.py \u2192 foo.py)
        test_basename = Path(test_path).stem
        for cf in changed_files:
            src_stem = Path(cf).stem
            if test_basename == f"test_{src_stem}" or test_basename == f"{src_stem}_test":
                reason_parts.append(f"name matches {src_stem}")

        if reason_parts:
            seen_paths.add(test_path)
            affected.append({
                "path": test_path,
                "reason": "; ".join(dict.fromkeys(reason_parts)),
            })

    return affected


def _filter_relevant_tests(
    root: Path,
    test_path: str,
    test_functions: list[str],
    changed_functions: list[str],
) -> list[str]:
    """Filter test functions to only those referencing changed functions.

    Reads the test file source and checks which test function bodies
    reference any of the changed function names.
    """
    if not changed_functions:
        return test_functions  # Can't filter \u2014 return all

    test_file = root / test_path
    try:
        source = test_file.read_text(encoding="utf-8", errors="replace")
        tree = parse_source_safe(source)
    except (OSError, UnicodeDecodeError):
        return test_functions

    if tree is None:
        return test_functions

    relevant: list[str] = []
    changed_set = set(changed_functions)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in test_functions:
            continue

        # Get source lines for this test function body
        try:
            body_source = ast.get_source_segment(source, node) or ""
        except (TypeError, AttributeError):
            # Fallback: extract by line range
            lines = source.splitlines()
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
            body_source = "\n".join(lines[start:end])

        # Check if any changed function is referenced in the test body
        if any(cf in body_source for cf in changed_set):
            relevant.append(node.name)

    return relevant if relevant else test_functions


def _find_coverage_gaps(
    source_defs: dict[str, dict[str, list[str]]],
    test_refs: dict[str, dict[str, Any]],
    changed_files: list[str],
    src_dir: str,
) -> list[dict[str, str]]:
    """Find functions/constants in changed files that no test references."""
    # Collect all references across all tests
    all_refs: set[str] = set()
    for refs in test_refs.values():
        all_refs.update(refs["references"])

    gaps = []

    # Only check changed files (or all source if no changes specified)
    files_to_check = changed_files if changed_files else list(source_defs.keys())

    for fpath in files_to_check:
        defs = source_defs.get(fpath)
        if defs is None:
            continue

        for func in defs["functions"]:
            # Skip private helpers and render functions (render is paired, not independently tested)
            if func.startswith("_"):
                continue
            if func not in all_refs:
                gaps.append({
                    "function": func,
                    "file": fpath,
                    "reason": "no test references this function",
                })

        for const in defs["constants"]:
            if const not in all_refs:
                gaps.append({
                    "function": const,
                    "file": fpath,
                    "reason": "no test references this constant",
                })

    return gaps


def _suggest_test_cases(
    changed_functions: list[str],
    source_defs: dict[str, dict[str, list[str]]],
    changed_files: list[str],
) -> list[str]:
    """Generate suggested test case names based on function names and patterns."""
    suggestions = []

    for func in changed_functions:
        if func.startswith("_"):
            # Private function \u2014 suggest integration-style test
            clean_name = func.lstrip("_").lower()
            suggestions.append(f"test_{clean_name}_basic_behavior")
        elif func.isupper() or (func.startswith("_") and func[1:].isupper()):
            # Constant \u2014 suggest value/presence test
            suggestions.append(f"test_{func.lower()}_is_defined")
            suggestions.append(f"test_{func.lower()}_values_are_valid")
        else:
            # Regular function \u2014 suggest happy path + edge cases
            suggestions.append(f"test_{func}_returns_expected_output")
            suggestions.append(f"test_{func}_handles_empty_input")

    # Deduplicate and limit
    suggestions = list(dict.fromkeys(suggestions))[:10]
    return suggestions
