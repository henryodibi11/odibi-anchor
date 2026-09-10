"""odibi_anchor.codebase.convention_preflight_context — Proactive convention guidance.

Before writing code, this tool produces the RELEVANT conventions that MUST be
followed. Unlike consistency_check_context (reactive, after code is written),
convention_preflight is proactive: it tells the agent what rules apply BEFORE
the first line is written.

Designed to prevent "agent violates conventions it already knows about" by
embedding the checklist directly into the pre-write context.

Usage:
    from odibi_anchor.codebase import convention_preflight_context

    ctx = convention_preflight_context(
        root="/path/to/project",
        action="new_function",
        target_file="src/my_pkg/validation/new_tool.py",
        function_name="anomaly_detection_context",
    )
    # ctx["checklist"] → list of conventions to follow
    # ctx["examples"] → extracted patterns from existing code

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
from odibi_anchor.codebase._convention_rules import run_anti_pattern_rules


_VALID_ACTIONS = {
    "new_function",
    "new_tool",         # context generator (stricter: needs render, contract keys)
    "new_module",
    "modify_function",
    "add_parameter",
    "refactor",
}

# Standard contract keys for context generators
_STANDARD_CONTRACT_KEYS = [
    "kind", "subject", "summary", "metrics", "findings",
    "risks", "samples", "suggested_next_actions",
]


def convention_preflight_context(
    root: str | Path,
    *,
    action: str = "new_function",
    target_file: str | None = None,
    function_name: str | None = None,
    changed_files: list[str] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> "dict[str, Any] | str":
    """Generate a proactive convention checklist before writing code.

    Scans the codebase to extract established patterns and produces a
    checklist of conventions that MUST be followed for the planned action.

    Args:
        root: Project root directory.
        action: Type of change planned. One of: "new_function",
            "new_tool", "new_module", "modify_function",
            "add_parameter", "refactor".
        target_file: Relative path to the file being created or modified.
        function_name: Name of the function being created or modified.
        changed_files: Optional list of relative paths to scan for anti-patterns.
            When provided, runs executable anti-pattern rules on these files.
        subject: Human label. Defaults to function_name or target file stem.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict or markdown string.

    Raises:
        ValueError: If parameters are invalid.

    Example:
        >>> ctx = convention_preflight_context(
        ...     "/path/to/project", action="new_tool",
        ...     function_name="anomaly_context")
        >>> "output_format parameter" in ctx["checklist"][0]["rule"]
        True
    """
    validate_output_format(output_format)
    if action not in _VALID_ACTIONS:
        raise ValueError(f"action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}")

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")

    subject = subject or function_name or (target_file.split("/")[-1] if target_file else action)

    # Scan existing patterns
    existing_patterns = _scan_existing_patterns(root)

    # Build checklist based on action
    checklist = _build_checklist(action, function_name, target_file, existing_patterns)

    # Build examples from existing code
    examples = _build_examples(action, function_name, existing_patterns)

    # Metrics
    metrics = {
        "checklist_item_count": len(checklist),
        "action": action,
        "patterns_scanned": existing_patterns.get("module_count", 0),
        "has_target_file": target_file is not None,
    }

    # Summary
    summary = (
        f"Preflight for {action}: {len(checklist)} convention(s) to follow. "
        f"Target: {subject}."
    )

    # Findings
    findings: list[str] = [
        f"Action: {action}",
        f"Checklist has {len(checklist)} item(s).",
    ]
    if function_name:
        findings.append(f"Function: {function_name}")
    if target_file:
        findings.append(f"Target file: {target_file}")

    # Risks (violations likely if not followed)
    risks: list[str] = []
    for item in checklist:
        if item.get("severity") == "blocker":
            risks.append(f"MUST: {item['rule']}")

    # ── Anti-pattern rules (Phase 1) ──
    anti_pattern_violations: list = []
    if changed_files:
        anti_pattern_violations = run_anti_pattern_rules(changed_files, str(root))
        if anti_pattern_violations:
            for v in anti_pattern_violations:
                loc = f" (line {v.line})" if v.line else ""
                findings.append(f"[{v.rule}] {v.file}{loc}: {v.message}")
                if v.severity == "blocker":
                    risks.append(f"BLOCKER: {v.message}")
            metrics["anti_pattern_violations"] = len(anti_pattern_violations)

    suggested_next_actions = [
        "Review checklist before writing code.",
        "Follow examples from existing tools for consistency.",
    ]


    # ── Graph wiring (audit fix) ──


    suggested_next_actions.append("MUST: Run anchor(\"lookup\", \"function_name\") to confirm patterns.")


    suggested_next_actions.append("MUST: Run anchor(\"touched\", \"file.py\") after implementing.")


    suggested_next_actions.append("MUST: Run anchor(\"preflight\") then anchor(\"test\") then anchor(\"gate\").")
    if action in {"new_tool", "new_function", "new_module"}:
        suggested_next_actions.append("Write tests alongside implementation.")

    suggested_next_actions.append("SKILL: Load skills/code-comprehension/SKILL.md when understanding existing conventions.")
    suggested_next_actions.append("SKILL: Load skills/writing-tests/SKILL.md when authoring test coverage.")

    ctx: dict[str, Any] = {
        "kind": "convention_preflight_context",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "checklist": checklist,
        "examples": examples,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "anti_pattern_violations": anti_pattern_violations,
        "suggested_next_actions": suggested_next_actions,
    }

    if output_format == "markdown":
        return render_convention_preflight_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Pattern Scanning
# ---------------------------------------------------------------------------


def _scan_existing_patterns(root: Path) -> dict[str, Any]:
    """Scan the codebase for established patterns."""
    patterns: dict[str, Any] = {
        "module_count": 0,
        "has_output_format": False,
        "has_render_functions": False,
        "has_standard_contract": False,
        "function_signatures": [],
        "render_function_names": [],
        "init_exports": [],
        "test_dirs": [],
    }

    src_dir = root / "src"
    if not src_dir.is_dir():
        src_dir = root

    py_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fname in filenames:
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)

    py_files.sort(key=lambda path: path.as_posix())
    patterns["module_count"] = len(py_files)

    for fpath in py_files:
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(fpath))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                if name.startswith("_"):
                    continue

                # Check for output_format parameter
                for arg in node.args.args + node.args.kwonlyargs:
                    if arg.arg == "output_format":
                        patterns["has_output_format"] = True

                # Check for render functions
                if name.startswith("render_") and name.endswith("_report"):
                    patterns["has_render_functions"] = True
                    patterns["render_function_names"].append(name)

                # Capture signature examples (first few)
                if name.endswith("_context") and len(patterns["function_signatures"]) < 5:
                    sig = _extract_signature(node)
                    patterns["function_signatures"].append({"name": name, "signature": sig})

        # Check __init__.py for exports
        if fpath.name == "__init__.py":
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__all__":
                            if isinstance(node.value, ast.List):
                                exports = [
                                    elt.value for elt in node.value.elts
                                    if isinstance(elt, ast.Constant)
                                ]
                                patterns["init_exports"].append({
                                    "file": str(fpath.relative_to(root)),
                                    "exports": exports,
                                })

    # Check for tests directory
    for candidate in ["tests", "test"]:
        if (root / candidate).is_dir():
            patterns["test_dirs"].append(candidate)

    return patterns


def _extract_signature(node: ast.FunctionDef) -> str:
    """Extract a function signature string from AST."""
    args = []
    for arg in node.args.args:
        args.append(arg.arg)
    for arg in node.args.kwonlyargs:
        args.append(f"{arg.arg}=...")
    return f"def {node.name}({', '.join(args[:8])})"


# ---------------------------------------------------------------------------
# Checklist Building
# ---------------------------------------------------------------------------


def _build_checklist(
    action: str,
    function_name: str | None,
    target_file: str | None,
    patterns: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the convention checklist based on action type."""
    checklist: list[dict[str, Any]] = []

    if action in {"new_tool", "new_function", "new_module"}:
        # Core conventions
        checklist.append({
            "rule": "Function must be standalone — no classes, no framework abstractions",
            "severity": "blocker",
            "category": "architecture",
        })
        checklist.append({
            "rule": "Must accept `output_format='dict'|'markdown'` parameter",
            "severity": "blocker",
            "category": "api",
        })

    if action == "new_tool":
        checklist.append({
            "rule": f"Must return standard contract keys: {', '.join(_STANDARD_CONTRACT_KEYS)}",
            "severity": "blocker",
            "category": "contract",
        })
        render_name = f"render_{function_name.replace('_context', '')}_report" if function_name else "render_<tool>_report"
        checklist.append({
            "rule": f"Must have paired render function: {render_name}(ctx)",
            "severity": "blocker",
            "category": "api",
        })
        checklist.append({
            "rule": "Must be exported from subpackage __init__.py (__all__)",
            "severity": "blocker",
            "category": "exports",
        })
        checklist.append({
            "rule": "Must have a test file: tests/test_<tool_name>.py",
            "severity": "blocker",
            "category": "testing",
        })
        checklist.append({
            "rule": "Must have a doc file: docs/tools/<tool_name>.md",
            "severity": "warning",
            "category": "documentation",
        })
        checklist.append({
            "rule": "Codebase tools: stdlib only. Data tools: pandas allowed, no other deps.",
            "severity": "blocker",
            "category": "dependencies",
        })

    if action in {"new_function", "new_module"}:
        checklist.append({
            "rule": "Public functions must have docstrings with Args/Returns/Raises",
            "severity": "warning",
            "category": "documentation",
        })
        checklist.append({
            "rule": "Use snake_case for functions, PascalCase for classes, UPPER_CASE for constants",
            "severity": "warning",
            "category": "naming",
        })

    if action == "add_parameter":
        checklist.append({
            "rule": "New parameters must be keyword-only with a default value (backward-compatible)",
            "severity": "blocker",
            "category": "api",
        })
        checklist.append({
            "rule": "Update docstring Args section with new parameter description",
            "severity": "warning",
            "category": "documentation",
        })
        checklist.append({
            "rule": "Add test coverage for new parameter behavior",
            "severity": "warning",
            "category": "testing",
        })

    if action == "modify_function":
        checklist.append({
            "rule": "Do not change return dict keys without updating all consumers",
            "severity": "blocker",
            "category": "contract",
        })
        checklist.append({
            "rule": "Run change_impact_context before modifying public functions",
            "severity": "warning",
            "category": "workflow",
        })

    if action == "refactor":
        checklist.append({
            "rule": "Run consistency_check_context after refactoring to catch drift",
            "severity": "warning",
            "category": "workflow",
        })
        checklist.append({
            "rule": "Ensure all tests pass after refactoring — no behavior changes",
            "severity": "blocker",
            "category": "testing",
        })

    # Universal
    checklist.append({
        "rule": "Validate output_format parameter early and raise ValueError if invalid",
        "severity": "warning",
        "category": "validation",
    })

    return checklist


def _build_examples(
    action: str,
    function_name: str | None,
    patterns: dict[str, Any],
) -> dict[str, Any]:
    """Build examples section from existing patterns."""
    examples: dict[str, Any] = {}

    if patterns.get("function_signatures"):
        examples["existing_signatures"] = patterns["function_signatures"][:3]

    if patterns.get("render_function_names"):
        examples["render_pattern"] = patterns["render_function_names"][:3]

    if action == "new_tool" and function_name:
        base = function_name.replace("_context", "")
        examples["suggested_signature"] = (
            f"def {function_name}(df, *, subject='dataframe', "
            f"engine='auto', output_format='dict') -> dict[str, Any] | str"
        )
        examples["suggested_render"] = f"def render_{base}_report(ctx: dict[str, Any]) -> str"
        examples["return_contract_keys"] = _STANDARD_CONTRACT_KEYS

    if patterns.get("init_exports"):
        examples["export_pattern"] = patterns["init_exports"][:2]

    return examples


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_convention_preflight_report(ctx: dict[str, Any]) -> str:
    """Render a convention preflight context dict as markdown.

    Args:
        ctx: Dictionary from ``convention_preflight_context()``.

    Returns:
        Markdown-formatted string.

    Raises:
        ValueError: If ctx is missing required keys.
    """
    required = {"kind", "subject", "summary", "checklist"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    lines.append(f"# Convention Preflight: {ctx['subject']}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Checklist
    lines.append("## Checklist")
    lines.append("")
    blockers = [c for c in ctx["checklist"] if c.get("severity") == "blocker"]
    warnings = [c for c in ctx["checklist"] if c.get("severity") == "warning"]

    if blockers:
        lines.append("### Must Follow (Blockers)")
        lines.append("")
        for item in blockers:
            lines.append(f"* \u274c {item['rule']}")
        lines.append("")

    if warnings:
        lines.append("### Should Follow (Warnings)")
        lines.append("")
        for item in warnings:
            lines.append(f"* \u26a0\ufe0f {item['rule']}")
        lines.append("")

    # Examples
    examples = ctx.get("examples", {})
    if examples:
        lines.append("## Examples from Existing Code")
        lines.append("")
        if examples.get("suggested_signature"):
            lines.append(f"**Signature:** `{examples['suggested_signature']}`")
            lines.append("")
        if examples.get("suggested_render"):
            lines.append(f"**Render:** `{examples['suggested_render']}`")
            lines.append("")
        if examples.get("return_contract_keys"):
            lines.append(f"**Contract keys:** {', '.join(examples['return_contract_keys'])}")
            lines.append("")

    return "\n".join(lines)
