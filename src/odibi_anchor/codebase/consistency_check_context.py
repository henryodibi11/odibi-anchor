"""odibi_anchor.codebase.consistency_check_context — Convention enforcement.

Scans a codebase and checks whether it follows its own established patterns.
Unlike code_pattern_context (discovery), this tool *enforces* conventions by
reporting violations.

Built-in rules detect:
- Missing __init__.py exports vs actual definitions
- Render function pairing (every *_context has a render_*_report)
- output_format parameter consistency across tools
- Docstring coverage on public functions
- Naming convention adherence (snake_case, UPPER_CASE constants)
- Test file coverage (every source module has tests)
- Return contract compliance (standard dict keys)

Usage:
    from odibi_anchor.codebase import consistency_check_context

    ctx = consistency_check_context("/path/to/project")
    if ctx["metrics"]["violation_count"] > 0:
        for v in ctx["violations"]:
            print(f"  {v['rule']}: {v['file']} — {v['detail']}")

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
from odibi_anchor._utils.ast_utils import walk_py_files, parse_source_safe, read_source_cached, parse_file_safe


def consistency_check_context(
    root: str | Path,
    *,
    subject: str | None = None,
    rules: list[str] | None = None,
    exclude_rules: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    custom_rules: list[dict[str, Any]] | None = None,
    changed_files: list[str] | None = None,
    scope: str = "all",
    suppress_suggestions: bool = False,
    output_format: str = "dict",
) -> "dict[str, Any] | str":
    """Check a codebase for internal consistency violations.

    Scans Python source files and checks whether the code follows its own
    established conventions. Reports violations with file, line, and detail.

    Args:
        root: Project root directory to scan.
        subject: Human label. Defaults to directory name.
        rules: List of rule names to check. If None, runs all built-in rules.
            Available: export_completeness, render_pairing, output_format_param,
            docstring_coverage, naming_conventions, test_coverage, return_contract.
        exclude_rules: Rules to skip (subtracted from active set).
        exclude_paths: Path patterns to exclude (e.g., ["tests/", "drafts/"]).
        custom_rules: Optional list of custom rule dicts. Each dict must have:
            - name (str): Rule identifier.
            - pattern (str): Regex pattern to flag in source files.
            - message (str): Violation message.
            - severity (str): "blocker" or "warning". Defaults to "warning".
            - file_filter (str): Optional glob pattern (e.g., "*.py"). Default: all .py files.
        changed_files: Optional list of recently changed file paths (relative to root).
            When provided, generates post_change_suggestions indicating which
            tools should be run based on what changed.
        scope: Controls violation output filtering. Options:
            - "all" (default): Report all violations found across the codebase.
            - "changed": Only report violations in files listed in changed_files.
              Still scans everything for cross-references but filters output.
        suppress_suggestions: If True, omits post_change_suggestions from output.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).

    Example:
        >>> ctx = consistency_check_context("/path/to/project")
        >>> ctx["metrics"]["violation_count"]
        3
        >>> ctx = consistency_check_context("/path/to/project", custom_rules=[
        ...     {"name": "no_spark_read", "pattern": r"spark[.]read[.]",
        ...      "message": "Use the project's approved data-access path", "severity": "blocker"}
        ... ])
    """
    validate_output_format(output_format)
    if scope not in ("all", "changed"):
        raise ValueError(f"scope must be 'all' or 'changed', got {scope!r}")

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")

    subject = subject or root.name
    exclude_paths = exclude_paths or []

    # Determine active rules
    all_rules = [
        "export_completeness",
        "render_pairing",
        "output_format_param",
        "docstring_coverage",
        "naming_conventions",
        "test_coverage",
        "return_contract",
    ]
    active_rules = rules if rules is not None else all_rules
    if exclude_rules:
        active_rules = [r for r in active_rules if r not in exclude_rules]

    # Scan source files
    source_files = _collect_python_files(root, exclude_paths)

    # Parse all files into AST info
    file_info = {}
    for fpath in source_files:
        rel = str(fpath.relative_to(root))
        info = _parse_file_info(fpath)
        if info is not None:
            file_info[rel] = info

    # Run each active rule
    violations: list[dict[str, Any]] = []
    compliant: list[dict[str, Any]] = []

    rule_runners = {
        "export_completeness": _check_export_completeness,
        "render_pairing": _check_render_pairing,
        "output_format_param": _check_output_format_param,
        "docstring_coverage": _check_docstring_coverage,
        "naming_conventions": _check_naming_conventions,
        "test_coverage": _check_test_coverage,
        "return_contract": _check_return_contract,
    }

    for rule_name in active_rules:
        runner = rule_runners.get(rule_name)
        if runner is None:
            continue
        rule_violations, rule_compliant = runner(root, file_info)
        violations.extend(rule_violations)
        compliant.extend(rule_compliant)

    # Run custom rules (regex-based pattern matching)
    if custom_rules:
        for custom_rule in custom_rules:
            _validate_custom_rule(custom_rule)
            custom_violations, custom_compliant = _run_custom_rule(
                root, source_files, custom_rule
            )
            violations.extend(custom_violations)
            compliant.extend(custom_compliant)

    # Apply scope filtering
    total_violations_found = len(violations)
    scoped_to_changed = (scope == "changed" and changed_files is not None)
    if scoped_to_changed:
        changed_set = {f.replace("\\", "/").lstrip("./") for f in changed_files}
        violations_in_scope = [
            v for v in violations
            if any(
                v.get("file", "").replace("\\", "/").lstrip("./").endswith(cf)
                or cf in v.get("file", "")
                for cf in changed_set
            )
        ]
        violations = violations_in_scope

    # Build metrics
    metrics = {
        "rules_checked": len(active_rules),
        "violation_count": len(violations),
        "compliant_count": len(compliant),
        "files_scanned": len(file_info),
        "violation_by_rule": _count_by_rule(violations),
        "scoped_to_changed": scoped_to_changed,
        "total_violations_found": total_violations_found,
        "violations_in_scope": len(violations),
    }

    # Summary
    if violations:
        summary = (
            f"{subject}: {len(violations)} violation(s) across "
            f"{len(metrics['violation_by_rule'])} rule(s). "
            f"{len(file_info)} files scanned."
        )
    else:
        summary = (
            f"{subject}: All {len(active_rules)} rules pass. "
            f"{len(file_info)} files scanned."
        )

    # Findings
    findings = _build_findings(violations, compliant, metrics)
    risks = [v["detail"] for v in violations[:5]] if violations else []

    # Suggested actions (MUST prefix for violations)
    suggested = []

    # ── Graph wiring (audit fix) ──

    suggested.append("MUST: Fix flagged inconsistencies before proceeding.")

    suggested.append("MUST: Run anchor(\"preflight\", changed_files=[...]) after fixes.")

    suggested.append("MUST: Run anchor(\"gate\", actions_taken=[...]) to close workflow.")
    for v in violations[:10]:
        suggested.append(f"MUST: Fix [{v['rule']}] in {v['file']}: {v['detail']}")

    # Post-change suggestions
    post_change_suggestions = []
    if changed_files and not suppress_suggestions:
        post_change_suggestions = _build_post_change_suggestions(
            root, changed_files, file_info
        )

    # Cap the compliant list — the full "everything that passed" list can dominate
    # the payload (it dwarfed the violations and pushed the output past the MCP
    # response limit), while compliant_count already conveys the total. Keep a
    # small sample for context.
    _COMPLIANT_SAMPLE = 25
    compliant_sample = compliant[:_COMPLIANT_SAMPLE]
    if len(compliant) > _COMPLIANT_SAMPLE:
        metrics["compliant_truncated"] = len(compliant) - _COMPLIANT_SAMPLE

    ctx: dict[str, Any] = {
        "kind": "consistency_check_context",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "violations": violations,
        "compliant": compliant_sample,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": suggested,
        "post_change_suggestions": post_change_suggestions,
    }

    if output_format == "markdown":
        return render_consistency_check_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Custom Rule Support
# ---------------------------------------------------------------------------


def _validate_custom_rule(rule: dict[str, Any]) -> None:
    """Validate a custom rule dict has required fields."""
    required = {"name", "pattern", "message"}
    missing = required - set(rule.keys())
    if missing:
        raise ValueError(f"Custom rule missing required keys: {sorted(missing)}. Got: {rule}")
    if not isinstance(rule["pattern"], str):
        raise ValueError(f"Custom rule 'pattern' must be a string regex, got {type(rule['pattern']).__name__}")


def _run_custom_rule(
    root: Path,
    source_files: list[Path],
    rule: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run a single custom regex rule against source files."""
    import fnmatch

    violations: list[dict[str, Any]] = []
    compliant: list[dict[str, Any]] = []

    rule_name = rule["name"]
    pattern = re.compile(rule["pattern"])
    message = rule["message"]
    severity = rule.get("severity", "warning")
    file_filter = rule.get("file_filter", "*.py")

    for fpath in source_files:
        rel = str(fpath.relative_to(root))

        # Apply file filter
        if file_filter and not fnmatch.fnmatch(fpath.name, file_filter):
            continue

        source = read_source_cached(fpath)
        if source is None:
            continue

        matches_found = False
        for line_num, line in enumerate(source.splitlines(), 1):
            if pattern.search(line):
                violations.append({
                    "rule": rule_name,
                    "file": rel,
                    "line": line_num,
                    "detail": message,
                    "severity": severity,
                    "match": line.strip()[:100],
                })
                matches_found = True

        if not matches_found:
            compliant.append({
                "rule": rule_name,
                "file": rel,
                "detail": f"No matches for pattern in {rel}",
            })

    return violations, compliant


# ---------------------------------------------------------------------------
# File Collection & Parsing
# ---------------------------------------------------------------------------

def _collect_python_files(
    root: Path, exclude_paths: list[str]
) -> list[Path]:
    """Collect all .py files, respecting exclusions."""
    return walk_py_files(root, exclude_paths=exclude_paths)


def _parse_file_info(fpath: Path) -> "dict[str, Any] | None":
    """Parse a Python file and extract structural info."""
    source = read_source_cached(fpath)
    if source is None:
        return None
    tree = parse_file_safe(fpath)
    if tree is None:
        return None

    info: dict[str, Any] = {
        "functions": [],
        "classes": [],
        "all_exports": None,
        "imports": [],
        "is_init": fpath.name == "__init__.py",
        "is_test": fpath.name.startswith("test_") or "/tests/" in str(fpath) or "\\tests\\" in str(fpath),
    }

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            func_info = _extract_function_info(node)
            info["functions"].append(func_info)

        elif isinstance(node, ast.ClassDef):
            class_info = {
                "name": node.name,
                "line": node.lineno,
                "methods": [],
                "docstring": ast.get_docstring(node),
            }
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_info["methods"].append(_extract_function_info(item))
            info["classes"].append(class_info)

        elif isinstance(node, ast.Assign):
            # Check for __all__
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        info["all_exports"] = [
                            elt.value for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        ]

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            info["imports"].append(node)

    return info


def _extract_function_info(node: ast.FunctionDef) -> dict[str, Any]:
    """Extract info about a function definition."""
    params = []
    has_output_format = False

    # Positional args
    for arg in node.args.args:
        name = arg.arg
        params.append(name)
        if name == "output_format":
            has_output_format = True

    # Keyword-only args
    for arg in node.args.kwonlyargs:
        name = arg.arg
        params.append(name)
        if name == "output_format":
            has_output_format = True

    return {
        "name": node.name,
        "line": node.lineno,
        "params": params,
        "has_output_format": has_output_format,
        "is_private": node.name.startswith("_"),
        "docstring": ast.get_docstring(node),
        "decorators": [_decorator_name(d) for d in node.decorator_list],
    }


def _decorator_name(node: ast.expr) -> str:
    """Extract decorator name as string."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return f"{_decorator_name(node.value)}.{node.attr}"
    elif isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return "?"


# ---------------------------------------------------------------------------
# Rule Checkers
# ---------------------------------------------------------------------------

def _check_export_completeness(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check that __all__ in __init__.py matches actual imports."""
    violations = []
    compliant = []

    for path, info in file_info.items():
        if not info["is_init"]:
            continue
        if info["all_exports"] is None:
            continue

        # Get names actually imported in the init
        imported_names: set[str] = set()
        for imp in info["imports"]:
            if isinstance(imp, ast.ImportFrom):
                for alias in imp.names:
                    imported_names.add(alias.asname or alias.name)

        # Get defined names in the init itself
        for func in info["functions"]:
            if not func["is_private"]:
                imported_names.add(func["name"])
        for cls in info["classes"]:
            imported_names.add(cls["name"])

        all_set = set(info["all_exports"])

        # In __all__ but not imported/defined
        orphaned = all_set - imported_names
        for name in sorted(orphaned):
            violations.append({
                "rule": "export_completeness",
                "file": path,
                "line": None,
                "detail": f"'{name}' in __all__ but not imported or defined",
                "severity": "warning",
            })

        # Imported but not in __all__
        missing = imported_names - all_set - {"__all__"}
        for name in sorted(missing):
            if not name.startswith("_"):
                violations.append({
                    "rule": "export_completeness",
                    "file": path,
                    "line": None,
                    "detail": f"'{name}' imported but not in __all__",
                    "severity": "info",
                })

        if not orphaned and not missing:
            compliant.append({
                "rule": "export_completeness",
                "file": path,
                "detail": "__all__ matches imports",
            })

    return violations, compliant


def _check_render_pairing(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check that every *_context function has a matching render_*_report."""
    violations = []
    compliant = []

    # Collect all public function names across non-test, non-init files
    context_funcs = {}  # name -> path
    render_funcs = set()

    for path, info in file_info.items():
        if info["is_test"] or info["is_init"]:
            continue
        # Skip utility modules — they provide helpers, not context tools
        if "/_utils/" in path or "\\_utils\\" in path:
            continue
        for func in info["functions"]:
            if func["is_private"]:
                continue
            name = func["name"]
            if name.endswith("_context"):
                context_funcs[name] = path
            if name.startswith("render_") and name.endswith("_report"):
                render_funcs.add(name)

    # Check pairing
    for ctx_name, path in context_funcs.items():
        # e.g., error_trace_context -> render_error_trace_report
        core = ctx_name.replace("_context", "")
        expected_render = f"render_{core}_report"
        if expected_render in render_funcs:
            compliant.append({
                "rule": "render_pairing",
                "file": path,
                "detail": f"{ctx_name} ↔ {expected_render}",
            })
        else:
            violations.append({
                "rule": "render_pairing",
                "file": path,
                "line": None,
                "detail": f"{ctx_name} has no matching {expected_render}",
                "severity": "warning",
            })

    return violations, compliant


def _check_output_format_param(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check that all *_context functions have output_format parameter."""
    violations = []
    compliant = []

    for path, info in file_info.items():
        if info["is_test"] or info["is_init"]:
            continue
        # Skip utility modules — they provide helpers, not context tools
        if "/_utils/" in path or "\\_utils\\" in path:
            continue
        for func in info["functions"]:
            if func["is_private"]:
                continue
            if not func["name"].endswith("_context"):
                continue
            if func["has_output_format"]:
                compliant.append({
                    "rule": "output_format_param",
                    "file": path,
                    "detail": f"{func['name']} has output_format",
                })
            else:
                violations.append({
                    "rule": "output_format_param",
                    "file": path,
                    "line": func["line"],
                    "detail": f"{func['name']} missing output_format parameter",
                    "severity": "warning",
                })

    return violations, compliant


def _check_docstring_coverage(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check that all public functions have docstrings."""
    violations = []
    compliant = []

    for path, info in file_info.items():
        if info["is_test"] or info["is_init"]:
            continue
        for func in info["functions"]:
            if func["is_private"]:
                continue
            if func["docstring"]:
                compliant.append({
                    "rule": "docstring_coverage",
                    "file": path,
                    "detail": f"{func['name']} has docstring",
                })
            else:
                violations.append({
                    "rule": "docstring_coverage",
                    "file": path,
                    "line": func["line"],
                    "detail": f"{func['name']} missing docstring",
                    "severity": "info",
                })

    return violations, compliant


def _check_naming_conventions(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check naming conventions: functions=snake_case, classes=PascalCase."""
    violations = []
    compliant = []

    snake_re = re.compile(r"^[a-z_][a-z0-9_]*$")
    pascal_re = re.compile(r"^[A-Z][a-zA-Z0-9]*$")

    for path, info in file_info.items():
        if info["is_test"] or info["is_init"]:
            continue

        for func in info["functions"]:
            name = func["name"]
            if name.startswith("__") and name.endswith("__"):
                continue  # dunder methods
            if snake_re.match(name):
                compliant.append({
                    "rule": "naming_conventions",
                    "file": path,
                    "detail": f"function {name} is snake_case",
                })
            else:
                violations.append({
                    "rule": "naming_conventions",
                    "file": path,
                    "line": func["line"],
                    "detail": f"function '{name}' is not snake_case",
                    "severity": "info",
                })

        for cls in info["classes"]:
            name = cls["name"]
            if pascal_re.match(name):
                compliant.append({
                    "rule": "naming_conventions",
                    "file": path,
                    "detail": f"class {name} is PascalCase",
                })
            else:
                violations.append({
                    "rule": "naming_conventions",
                    "file": path,
                    "line": cls["line"],
                    "detail": f"class '{name}' is not PascalCase",
                    "severity": "info",
                })

    return violations, compliant


def _check_test_coverage(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check that every source module has at least one test file."""
    violations = []
    compliant = []

    # Collect source files (non-test, non-init)
    source_files = {}
    test_files = set()

    for path, info in file_info.items():
        if info["is_init"]:
            continue
        if info["is_test"]:
            test_files.add(path)
        else:
            source_files[path] = info

    for src_path in source_files:
        # Derive expected test name
        src_name = Path(src_path).stem  # e.g., "error_trace_context"
        expected_test = f"test_{src_name}"

        # Check if any test file matches
        has_test = any(
            expected_test in tf for tf in test_files
        )
        if has_test:
            compliant.append({
                "rule": "test_coverage",
                "file": src_path,
                "detail": f"Has matching test file",
            })
        else:
            violations.append({
                "rule": "test_coverage",
                "file": src_path,
                "line": None,
                "detail": f"No test file found matching 'test_{src_name}'",
                "severity": "warning",
            })

    return violations, compliant


def _check_return_contract(
    root: Path, file_info: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Check that *_context functions return dicts with standard keys.

    This does best-effort AST extraction of return statement dict keys.
    Only checks functions that have a detectable return dict.
    """
    violations = []
    compliant = []

    required_keys = {"kind", "subject", "summary", "metrics", "findings", "risks"}

    for path, info in file_info.items():
        if info["is_test"] or info["is_init"]:
            continue
        # Skip utility modules — they provide helpers, not context tools
        if "/_utils/" in path or "\\_utils\\" in path:
            continue

        fpath = root / path
        source = read_source_cached(fpath)
        if source is None:
            continue
        tree = parse_file_safe(fpath)
        if tree is None:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.endswith("_context"):
                continue
            if node.name.startswith("_"):
                continue

            # Find return statements with dict literals
            return_keys = _extract_return_dict_keys(node)
            if not return_keys:
                continue  # Can't analyze, skip

            missing = required_keys - return_keys
            if missing:
                violations.append({
                    "rule": "return_contract",
                    "file": path,
                    "line": node.lineno,
                    "detail": (
                        f"{node.name} missing contract keys: "
                        f"{sorted(missing)}"
                    ),
                    "severity": "blocker",
                })
            else:
                compliant.append({
                    "rule": "return_contract",
                    "file": path,
                    "detail": f"{node.name} returns all contract keys",
                })

    return violations, compliant


def _extract_return_dict_keys(func_node: ast.FunctionDef) -> set[str]:
    """Extract keys from return dict in a function (best-effort)."""
    keys: set[str] = set()

    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
            elif isinstance(node.value, ast.Name):
                # Look for `ctx = {...}` then `return ctx`
                var_name = node.value.id
                keys.update(_find_dict_var_keys(func_node, var_name))

    return keys


def _find_dict_var_keys(func_node: ast.FunctionDef, var_name: str) -> set[str]:
    """Find keys assigned to a variable dict in function body."""
    keys: set[str] = set()

    for node in ast.walk(func_node):
        # Regular assignment: ctx = {...}
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                keys.add(key.value)
                # Subscript assignment: ctx["key"] = ...
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == var_name
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    keys.add(target.slice.value)

        # Annotated assignment: ctx: dict[str, Any] = {...}
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name)
                    and node.target.id == var_name
                    and node.value is not None
                    and isinstance(node.value, ast.Dict)):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)

    return keys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_by_rule(violations: list[dict]) -> dict[str, int]:
    """Count violations by rule name."""
    counts: dict[str, int] = {}
    for v in violations:
        rule = v["rule"]
        counts[rule] = counts.get(rule, 0) + 1
    return counts


def _build_findings(
    violations: list[dict],
    compliant: list[dict],
    metrics: dict[str, Any],
) -> list[str]:
    """Build findings summary."""
    findings = []

    if violations:
        findings.append(
            f"{metrics['violation_count']} violation(s) found "
            f"across {len(metrics['violation_by_rule'])} rule(s)."
        )
        for rule, count in sorted(metrics["violation_by_rule"].items()):
            findings.append(f"  {rule}: {count} violation(s)")
    else:
        findings.append("All consistency checks pass.")

    findings.append(
        f"{metrics['compliant_count']} convention(s) confirmed compliant."
    )
    return findings


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Post-Change Suggestions
# ---------------------------------------------------------------------------


def _build_post_change_suggestions(
    root: Path,
    changed_files: list[str],
    file_info: dict[str, Any],
) -> list[dict[str, str]]:
    """Generate tool suggestions based on what files changed.

    Uses MUST/SHOULD priority and includes suggested_call for each tool.
    """
    suggestions: list[dict[str, str]] = []
    seen_tools: set[str] = set()

    src_files = [f for f in changed_files if f.startswith("src/") and f.endswith(".py")]
    init_files = [f for f in changed_files if f.endswith("__init__.py")]
    test_files_changed = [f for f in changed_files if "test" in f.lower()]
    files_repr = repr(src_files)

    # If source files modified → suggest test_focus_context
    if src_files and "test_focus_context" not in seen_tools:
        suggestions.append({
            "tool": "test_focus_context",
            "reason": f"{len(src_files)} source file(s) modified — find affected tests",
            "priority": "MUST",
            "suggested_call": f"test_focus_context(root, changed_files={files_repr})",
        })
        seen_tools.add("test_focus_context")

    # Check for shared/imported functions in changed files
    has_shared_functions = False
    for fpath in src_files:
        info = file_info.get(fpath)
        if info is None:
            continue
        # If the file has functions that other files import
        functions = [f.get("name", "") for f in info.get("functions", [])]
        if any(not f.startswith("_") for f in functions):
            has_shared_functions = True
            break

    if has_shared_functions and "change_impact_context" not in seen_tools:
        first_file = src_files[0] if src_files else ""
        suggestions.append({
            "tool": "change_impact_context",
            "reason": "shared function(s) modified — check downstream impact",
            "priority": "MUST",
            "suggested_call": f"change_impact_context(root, target='{first_file}', change_type='modify')",
        })
        seen_tools.add("change_impact_context")

    # Check for new functions/constants
    has_new_definitions = False
    for fpath in src_files:
        info = file_info.get(fpath)
        if info is not None:
            has_new_definitions = True
            break

    if has_new_definitions and "convention_preflight_context" not in seen_tools:
        suggestions.append({
            "tool": "convention_preflight_context",
            "reason": "source definitions changed — verify naming/style conventions",
            "priority": "SHOULD",
            "suggested_call": f"convention_preflight_context(root, function_name='...', target_file='{src_files[0] if src_files else ''}')",
        })
        seen_tools.add("convention_preflight_context")

    # If tool output behavior likely changed (profiling/ or any *_context.py)
    behavior_files = [
        f for f in src_files
        if "context" in f.lower() or "profiling" in f.lower()
    ]
    if behavior_files and "dogfood_regression_context" not in seen_tools:
        suggestions.append({
            "tool": "dogfood_regression_context",
            "reason": "tool behavior may have changed — check for regressions",
            "priority": "SHOULD",
            "suggested_call": "dogfood_regression_context(output, root=root)",
        })
        seen_tools.add("dogfood_regression_context")

    # If __init__.py changed → verify exports
    if init_files:
        suggestions.append({
            "tool": "consistency_check_context",
            "reason": "__init__.py changed — verify export completeness",
            "priority": "SHOULD",
            "suggested_call": f"consistency_check_context(root, changed_files={repr(init_files)})",
        })

    return suggestions


def render_consistency_check_report(ctx: dict[str, Any]) -> str:
    """Render a consistency check as a markdown report.

    Args:
        ctx: Dictionary from consistency_check_context().

    Returns:
        Human-readable markdown string.

    Raises:
        ValueError: If required keys are missing.
    """
    required = {"kind", "subject", "summary", "metrics"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    metrics = ctx["metrics"]
    violations = ctx.get("violations", [])

    # Header
    status = "PASS" if metrics["violation_count"] == 0 else "VIOLATIONS FOUND"
    lines.append(f"# Consistency Check: {ctx['subject']} — {status}")
    lines.append("")
    lines.append(f"> {ctx['summary']}")
    lines.append("")

    # Metrics table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Rules checked | {metrics['rules_checked']} |")
    lines.append(f"| Files scanned | {metrics['files_scanned']} |")
    lines.append(f"| Violations | {metrics['violation_count']} |")
    lines.append(f"| Compliant | {metrics['compliant_count']} |")
    lines.append("")

    # Violations
    if violations:
        lines.append("## Violations")
        lines.append("")

        # Group by rule
        by_rule: dict[str, list[dict]] = {}
        for v in violations:
            by_rule.setdefault(v["rule"], []).append(v)

        for rule, items in sorted(by_rule.items()):
            lines.append(f"### {rule} ({len(items)})")
            lines.append("")
            for v in items:
                loc = f"L{v['line']}" if v.get("line") else ""
                lines.append(f"- `{v['file']}` {loc}: {v['detail']}")
            lines.append("")
    else:
        lines.append("## All Checks Pass")
        lines.append("")
        lines.append("No consistency violations detected.")
        lines.append("")

    # Suggested actions
    suggested = ctx.get("suggested_next_actions", [])
    if suggested:
        lines.append("## Suggested Actions")
        lines.append("")
        for i, action in enumerate(suggested[:10], 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    return "\n".join(lines)
