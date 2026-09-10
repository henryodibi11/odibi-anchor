"""odibi_anchor.codebase.change_impact_context — Ripple analysis for code changes.

Given a target file or function you're about to change, maps everything that
depends on it: importers, call sites, test files, documentation references.
Produces a risk assessment and update checklist.

Designed to eliminate the "grep → read → grep" cycle — the 3-5 tool calls
an AI agent performs to understand what a change will affect.

Usage:
    from odibi_anchor.codebase import change_impact_context

    ctx = change_impact_context(
        root="/path/to/project",
        target="src/my_pkg/validation/quality_gate_context.py",
        function="quality_gate_context",
        change_type="add_parameter",
    )
    print(ctx["summary"])
    # "quality_gate_context: 3 importers, 2 test files, 1 doc. LOW risk (backward-compatible)."

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


# Change types and their risk profiles
_CHANGE_RISK = {
    "add_parameter": "low",        # New kwarg with default → backward-compatible
    "add_required_parameter": "high",  # Breaking: all callers must update
    "remove_parameter": "high",    # Breaking: callers using it will fail
    "rename_parameter": "high",    # Breaking: keyword callers will fail
    "rename_function": "high",     # Breaking: all importers must update
    "change_return_type": "medium",  # May break callers depending on usage
    "change_return_shape": "medium", # Dict key changes may break consumers
    "add_function": "low",         # No existing code affected
    "delete_function": "high",     # All callers break
    "modify_logic": "low",        # Internal change, interface stable
    "refactor": "medium",         # May change behavior
}

# Infrastructure files that create non-import dependencies
_INFRA_FILES: list[dict[str, Any]] = [
    {
        "file_glob": "**/conftest.py",
        "label": "test configuration",
        "triggers_on": ["rename_function", "delete_function", "refactor",
                        "add_parameter", "remove_parameter"],
        "note": "May contain fixtures or hooks referencing the target",
    },
    {
        "file_glob": "pyproject.toml",
        "label": "project configuration",
        "triggers_on": ["rename_function", "refactor"],
        "note": "Entry points, test config, or build config may reference the target",
    },
    {
        "file_glob": "**/__init__.py",
        "label": "package exports",
        "triggers_on": ["rename_function", "delete_function", "refactor"],
        "note": "Re-exports must be updated",
    },
    {
        "file_glob": ".agent_memory.db",
        "label": "agent memory",
        "triggers_on": ["rename_function", "delete_function", "refactor",
                        "change_return_shape", "add_parameter", "remove_parameter"],
        "note": "Documented patterns/gotchas may reference old name or signature",
    },
    {
        "file_glob": "CHANGELOG.md",
        "label": "changelog",
        "triggers_on": ["rename_function", "delete_function", "add_function",
                        "change_return_shape", "add_parameter", "remove_parameter"],
        "note": "Document the change",
    },
    {
        "file_glob": "scripts/*.py",
        "label": "scripts",
        "triggers_on": ["rename_function", "delete_function", "refactor",
                        "change_return_shape"],
        "note": "Build/generate scripts may import or reference the target",
    },
]


def change_impact_context(
    root: str | Path,
    *,
    target: str,
    function: str | None = None,
    change_type: str = "modify_logic",
    subject: str | None = None,
    include_docs: bool = True,
    doc_dirs: list[str] | None = None,
    include_infra: bool = True,
    output_format: str = "dict",
) -> "dict[str, Any] | str":
    """Analyze the impact of changing a file or function.

    Scans the project for all references to the target, assesses risk,
    and produces an update checklist.

    Args:
        root: Project root directory.
        target: Relative path to the file being changed (from root).
        function: Optional specific function name within the target file.
            When set, analysis focuses on that function's callers.
        change_type: Type of change being made. Determines risk level.
            Options: add_parameter, add_required_parameter, remove_parameter,
            rename_parameter, rename_function, change_return_type,
            change_return_shape, add_function, delete_function,
            modify_logic, refactor.
        subject: Human label. Defaults to function name or file stem.
        include_docs: If True, scan documentation directories for references.
        doc_dirs: Directory names to scan for docs. Defaults to
            ["docs", "skills"].
        include_infra: If True, scan infrastructure files (conftest, pyproject,
            __init__.py, .agent_memory.md, CHANGELOG, scripts/) for references.
            Defaults to True.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict or markdown string.
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")

    target_path = root / target
    try:
        target_path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"Target path escapes project root: {target}")
    if not target_path.exists():
        raise ValueError(f"target file not found: {target}")

    if doc_dirs is None:
        doc_dirs = ["docs", "skills"]

    subject = subject or function or target_path.stem
    target_stem = target_path.stem

    # Determine search terms
    search_terms = _build_search_terms(target_stem, function)

    # Find all .py files (excluding target itself)
    py_files = _find_py_files(root, exclude=target_path)

    # Find importers (files that import from target)
    importers = _find_importers(py_files, root, target_stem, function)

    # Find call sites (lines where the function is actually called)
    call_sites: list[dict[str, Any]] = []
    if function:
        call_sites = _find_call_sites(py_files, root, function)

    # Find test files
    test_files = _find_test_references(py_files, root, search_terms)

    # Find documentation references
    doc_references: list[dict[str, Any]] = []
    if include_docs:
        doc_references = _find_doc_references(root, doc_dirs, search_terms)

    # Find infrastructure references
    infra_references: list[dict[str, Any]] = []
    if include_infra:
        infra_references = _find_infra_references(root, search_terms, change_type)

    # Risk assessment
    base_risk = _CHANGE_RISK.get(change_type, "medium")
    risk_assessment = _assess_risk(
        base_risk=base_risk,
        change_type=change_type,
        importer_count=len(importers),
        test_count=len(test_files),
        call_site_count=len(call_sites),
    )

    # Build update checklist
    checklist = _build_checklist(
        change_type=change_type,
        function=function,
        importers=importers,
        test_files=test_files,
        doc_references=doc_references,
        call_sites=call_sites,
        infra_references=infra_references,
    )

    # Metrics
    metrics = {
        "target": target,
        "function": function,
        "change_type": change_type,
        "importer_count": len(importers),
        "call_site_count": len(call_sites),
        "test_file_count": len(test_files),
        "doc_reference_count": len(doc_references),
        "infra_reference_count": len(infra_references),
        "risk_level": risk_assessment["level"],
        "is_breaking": risk_assessment["is_breaking"],
    }

    # Summary
    parts = []
    if importers:
        parts.append(f"{len(importers)} importer(s)")
    if test_files:
        parts.append(f"{len(test_files)} test file(s)")
    if doc_references:
        parts.append(f"{len(doc_references)} doc(s)")
    if call_sites:
        parts.append(f"{len(call_sites)} call site(s)")
    if infra_references:
        parts.append(f"{len(infra_references)} infra ref(s)")
    refs = ", ".join(parts) if parts else "no references found"
    summary = (
        f"{subject}: {refs}. "
        f"{risk_assessment['level'].upper()} risk"
        f"{' (backward-compatible)' if not risk_assessment['is_breaking'] else ' (BREAKING)'}."
    )

    # Findings
    findings = _build_findings(importers, test_files, doc_references, call_sites, risk_assessment, infra_references)
    risks = _build_risks(risk_assessment, importers, test_files, change_type)
    suggested_next_actions = checklist

    # ── Graph wiring (audit fix) ──

    suggested_next_actions += [

        'MUST: Run anchor("test", changed_files=[...]) to verify affected modules.',

        'SHOULD: Run anchor("preflight", changed_files=[...]) for syntax/type errors.',

        'SHOULD: Run anchor("consistency") if __init__.py exports may have drifted.',

    ]

    # ── Cap heavy lists with size hints (AXI content truncation) ──
    # Counts in metrics (importer_count/call_site_count) stay accurate above.
    from odibi_anchor._utils.contract import cap_with_hint
    importers, _imp_hint = cap_with_hint(importers, 30, unit="importers")
    call_sites, _cs_hint = cap_with_hint(call_sites, 30, unit="call sites")
    for _h in (_imp_hint, _cs_hint):
        if _h:
            findings.append(_h)

    ctx: dict[str, Any] = {
        "kind": "change_impact_context",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "risk_assessment": risk_assessment,
        "importers": importers,
        "call_sites": call_sites,
        "test_files": test_files,
        "doc_references": doc_references,
        "infra_references": infra_references,
        "checklist": checklist,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": suggested_next_actions,
    }

    if output_format == "markdown":
        return render_change_impact_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Search Term Building
# ---------------------------------------------------------------------------

def _build_search_terms(target_stem: str, function: str | None) -> list[str]:
    """Build list of terms to search for in files."""
    terms = [target_stem]
    if function:
        terms.append(function)
    return terms


# ---------------------------------------------------------------------------
# File Discovery
# ---------------------------------------------------------------------------

def _find_py_files(root: Path, *, exclude: Path) -> list[Path]:
    """Find all .py files under root, excluding the target."""
    all_files = walk_py_files(root)
    # Use str comparison to avoid expensive resolve() on FUSE filesystems
    exclude_str = str(exclude)
    return [f for f in all_files if str(f) != exclude_str]


# ---------------------------------------------------------------------------
# Import Analysis
# ---------------------------------------------------------------------------

def _find_importers(
    files: list[Path], root: Path, target_stem: str, function: str | None
) -> list[dict[str, Any]]:
    """Find files that import from the target module."""
    importers: list[dict[str, Any]] = []

    for filepath in files:
        source = read_source_cached(filepath)
        if source is None:
            continue
        tree = parse_file_safe(filepath)
        if tree is None:
            continue

        rel = str(filepath.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # Check if import references our target module
                if target_stem in module.split("."):
                    imported_names = [a.name for a in node.names]
                    usage = "re-export" if _is_init(filepath) else "import"
                    # If function specified, check if it's specifically imported
                    if function:
                        if function in imported_names or "*" in imported_names:
                            importers.append({
                                "file": rel,
                                "line": node.lineno,
                                "imported_names": imported_names,
                                "usage": usage,
                            })
                    else:
                        importers.append({
                            "file": rel,
                            "line": node.lineno,
                            "imported_names": imported_names,
                            "usage": usage,
                        })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if target_stem in alias.name.split("."):
                        importers.append({
                            "file": rel,
                            "line": node.lineno,
                            "imported_names": [alias.name],
                            "usage": "import",
                        })

    return importers


def _is_init(filepath: Path) -> bool:
    """Check if file is an __init__.py."""
    return filepath.name == "__init__.py"


# ---------------------------------------------------------------------------
# Call Site Analysis
# ---------------------------------------------------------------------------

def _find_call_sites(
    files: list[Path], root: Path, function: str
) -> list[dict[str, Any]]:
    """Find lines where a specific function is called."""
    call_sites: list[dict[str, Any]] = []
    pattern = re.compile(rf"\b{re.escape(function)}\s*\(")

    for filepath in files:
        source = read_source_cached(filepath)
        if source is None:
            continue

        rel = str(filepath.relative_to(root))
        for i, line in enumerate(source.splitlines(), 1):
            if pattern.search(line):
                call_sites.append({
                    "file": rel,
                    "line": i,
                    "code": line.strip()[:120],
                })

    return call_sites


# ---------------------------------------------------------------------------
# Test References
# ---------------------------------------------------------------------------

def _find_test_references(
    files: list[Path], root: Path, search_terms: list[str]
) -> list[dict[str, Any]]:
    """Find test files that reference the target."""
    test_files: list[dict[str, Any]] = []

    for filepath in files:
        rel = str(filepath.relative_to(root))
        # Only consider test files
        if not (filepath.name.startswith("test_") or
                filepath.name.endswith("_test.py") or
                "tests" in filepath.parts or
                "test" in filepath.parts):
            continue

        source = read_source_cached(filepath)
        if source is None:
            continue

        # Check if any search term appears
        matches = [t for t in search_terms if t in source]
        if matches:
            # Count test functions
            tree = parse_file_safe(filepath)
            if tree:
                test_count = sum(
                    1 for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
                )
            else:
                test_count = 0

            test_files.append({
                "file": rel,
                "matched_terms": matches,
                "test_count": test_count,
            })

    return test_files


# ---------------------------------------------------------------------------
# Documentation References
# ---------------------------------------------------------------------------

def _find_doc_references(
    root: Path, doc_dirs: list[str], search_terms: list[str]
) -> list[dict[str, Any]]:
    """Find documentation files that reference the target."""
    doc_refs: list[dict[str, Any]] = []
    doc_extensions = {".md", ".rst", ".txt", ".yaml", ".yml"}

    for doc_dir in doc_dirs:
        dir_path = root / doc_dir
        if not dir_path.is_dir():
            continue
        for dirpath, _, filenames in os.walk(dir_path):
            for fname in filenames:
                if Path(fname).suffix in doc_extensions:
                    filepath = Path(dirpath) / fname
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except (UnicodeDecodeError, OSError):
                        continue

                    matches = [t for t in search_terms if t in content]
                    if matches:
                        # Find which sections mention it
                        sections = _find_doc_sections(content, matches)
                        rel = str(filepath.relative_to(root))
                        doc_refs.append({
                            "file": rel,
                            "matched_terms": matches,
                            "sections": sections,
                        })

    return doc_refs


def _find_doc_sections(content: str, terms: list[str]) -> list[str]:
    """Find markdown section headers near term occurrences."""
    sections: list[str] = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        for term in terms:
            if term in line:
                # Look backward for nearest header
                for j in range(i, max(i - 20, -1), -1):
                    if lines[j].startswith("#"):
                        header = lines[j].lstrip("#").strip()
                        if header not in sections:
                            sections.append(header)
                        break
                break
    return sections[:5]  # Cap at 5


# ---------------------------------------------------------------------------
# Infrastructure References
# ---------------------------------------------------------------------------

def _find_infra_references(
    root: Path,
    search_terms: list[str],
    change_type: str,
) -> list[dict[str, Any]]:
    """Find infrastructure files that reference the target.

    Scans non-Python config/doc files that create implicit dependencies
    not visible through AST import analysis.

    Uses fnmatch instead of glob to avoid expensive recursive scandir
    on FUSE-mounted workspace filesystems.
    """
    import fnmatch as fnmatch_mod

    infra_refs: list[dict[str, Any]] = []
    infra_extensions = {".py", ".toml", ".yaml", ".yml", ".md", ".cfg", ".ini", ".txt"}

    # Collect relevant rules for this change type
    active_rules = [r for r in _INFRA_FILES if change_type in r["triggers_on"]]
    if not active_rules:
        return []

    # Walk the tree once (cheap — reuses os.walk, no stat per file)
    all_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs and __pycache__
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix in infra_extensions:
                all_files.append(fpath)

    for rule in active_rules:
        glob_pattern = rule["file_glob"]
        for filepath in all_files:
            rel = str(filepath.relative_to(root))
            if not fnmatch_mod.fnmatch(rel, glob_pattern):
                continue

            source = read_source_cached(filepath) if filepath.suffix == ".py" else None
            if source is None:
                try:
                    source = filepath.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeDecodeError):
                    continue

            # Check if any search term appears
            matches = [t for t in search_terms if t in source]
            if matches:
                infra_refs.append({
                    "file": rel,
                    "label": rule["label"],
                    "matched_terms": matches,
                    "note": rule["note"],
                })

    # Deduplicate by file path
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for ref in infra_refs:
        if ref["file"] not in seen:
            seen.add(ref["file"])
            deduped.append(ref)
    return deduped


# ---------------------------------------------------------------------------
# Risk Assessment
# ---------------------------------------------------------------------------

def _assess_risk(
    *,
    base_risk: str,
    change_type: str,
    importer_count: int,
    test_count: int,
    call_site_count: int,
) -> dict[str, Any]:
    """Produce a risk assessment dict."""
    is_breaking = base_risk == "high" or (
        base_risk == "medium" and call_site_count > 5
    )

    # Adjust risk based on test coverage
    if is_breaking and test_count == 0:
        confidence = "low"
        note = "No tests cover this — breakage may go undetected."
    elif is_breaking and test_count > 0:
        confidence = "high"
        note = f"Tests exist ({test_count} file(s)) — breakage will be caught."
    elif not is_breaking and importer_count == 0:
        confidence = "high"
        note = "No importers — change is isolated."
    else:
        confidence = "medium"
        note = "Change is backward-compatible but verify behavior."

    return {
        "level": base_risk,
        "is_breaking": is_breaking,
        "confidence": confidence,
        "note": note,
        "change_type": change_type,
    }


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------

def _build_checklist(
    *,
    change_type: str,
    function: str | None,
    importers: list[dict],
    test_files: list[dict],
    doc_references: list[dict],
    call_sites: list[dict],
    infra_references: list[dict] | None = None,
) -> list[str]:
    """Build an actionable update checklist with MUST/SHOULD prefixes."""
    checklist: list[str] = []

    if change_type in ("rename_function", "delete_function"):
        for imp in importers:
            checklist.append(f"MUST: Update import in {imp['file']} (line {imp['line']})")
        for cs in call_sites:
            checklist.append(f"MUST: Update call in {cs['file']} (line {cs['line']})")

    elif change_type in ("add_required_parameter", "remove_parameter", "rename_parameter"):
        for cs in call_sites:
            checklist.append(f"MUST: Update call in {cs['file']} (line {cs['line']})")

    elif change_type in ("change_return_type", "change_return_shape"):
        for cs in call_sites:
            checklist.append(f"MUST: Verify return usage in {cs['file']} (line {cs['line']})")

    elif change_type == "add_parameter":
        # Backward-compatible — just update docs and tests
        pass

    # Always suggest doc updates
    for doc in doc_references:
        checklist.append(f"MUST: Update documentation: {doc['file']}")

    # Always suggest test updates
    if test_files:
        checklist.append(f"MUST: Update/add tests in {len(test_files)} test file(s)")
    elif function:
        checklist.append(f"MUST: Write tests for {function} (none found)")

    # Re-export check
    for imp in importers:
        if imp["usage"] == "re-export":
            if change_type == "rename_function":
                checklist.append(f"MUST: Update re-export in {imp['file']}")

    # Infrastructure references
    for infra in (infra_references or []):
        checklist.append(f"MUST: Check {infra['label']}: {infra['file']} \u2014 {infra['note']}")

    # anchor() workflow hints
    checklist.append(
        "MUST: Run anchor('preflight', changed_files=[...]) after all updates to catch remaining issues."
    )
    if test_files or function:
        checklist.append(
            "MUST: Run anchor('test', changed_files=[...]) to confirm test coverage for impacted files."
        )
    return checklist


# ---------------------------------------------------------------------------
# Findings & Risks
# ---------------------------------------------------------------------------

def _build_findings(
    importers: list[dict],
    test_files: list[dict],
    doc_references: list[dict],
    call_sites: list[dict],
    risk_assessment: dict,
    infra_references: list[dict] | None = None,
) -> list[str]:
    """Build findings list."""
    findings: list[str] = []

    if importers:
        re_exports = [i for i in importers if i["usage"] == "re-export"]
        direct = [i for i in importers if i["usage"] != "re-export"]
        if re_exports:
            findings.append(f"Re-exported from {len(re_exports)} __init__.py file(s).")
        if direct:
            findings.append(f"Directly imported in {len(direct)} file(s).")
    else:
        findings.append("No importers found — this may be an internal/unused module.")

    if call_sites:
        files_with_calls = len(set(cs["file"] for cs in call_sites))
        findings.append(f"{len(call_sites)} call site(s) across {files_with_calls} file(s).")

    if test_files:
        total_tests = sum(t["test_count"] for t in test_files)
        findings.append(f"Covered by {total_tests} test(s) in {len(test_files)} file(s).")
    else:
        findings.append("No test coverage found for this target.")

    if doc_references:
        findings.append(f"Referenced in {len(doc_references)} documentation file(s).")

    if infra_references:
        findings.append(
            f"Referenced in {len(infra_references)} infrastructure file(s) "
            f"(config, scripts, docs)."
        )

    findings.append(f"Risk: {risk_assessment['level'].upper()} \u2014 {risk_assessment['note']}")

    return findings


def _build_risks(
    risk_assessment: dict,
    importers: list[dict],
    test_files: list[dict],
    change_type: str,
) -> list[str]:
    """Build risk list."""
    risks: list[str] = []

    if risk_assessment["is_breaking"]:
        risks.append(
            f"BREAKING CHANGE ({change_type}): all callers must be updated."
        )

    if not test_files:
        risks.append("No test coverage — breakage may go undetected.")

    if risk_assessment["is_breaking"] and len(importers) > 5:
        risks.append(
            f"High fan-out: {len(importers)} files import this. "
            f"Consider a deprecation path."
        )

    return risks


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_change_impact_report(ctx: dict[str, Any]) -> str:
    """Render a change impact context dict as a markdown report.

    Args:
        ctx: Dictionary from change_impact_context().

    Returns:
        Human-readable markdown string.

    Raises:
        ValueError: If required keys are missing.
    """
    required = {"kind", "subject", "summary", "metrics", "risk_assessment"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    metrics = ctx["metrics"]
    risk = ctx["risk_assessment"]

    # Header
    lines.append(f"# Change Impact: {ctx['subject']}")
    lines.append("")
    lines.append(f"> {ctx['summary']}")
    lines.append("")

    # Risk badge
    level = risk["level"].upper()
    breaking = "BREAKING" if risk["is_breaking"] else "backward-compatible"
    lines.append(f"**Risk: {level}** ({breaking})")
    lines.append(f"")
    lines.append(f"*{risk['note']}*")
    lines.append("")

    # Metrics
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Target | `{metrics['target']}` |")
    if metrics["function"]:
        lines.append(f"| Function | `{metrics['function']}` |")
    lines.append(f"| Change type | `{metrics['change_type']}` |")
    lines.append(f"| Importers | {metrics['importer_count']} |")
    lines.append(f"| Call sites | {metrics['call_site_count']} |")
    lines.append(f"| Test files | {metrics['test_file_count']} |")
    lines.append(f"| Doc references | {metrics['doc_reference_count']} |")
    lines.append(f"| Infra references | {metrics['infra_reference_count']} |")
    lines.append("")

    # Importers
    importers = ctx.get("importers", [])
    if importers:
        lines.append("## Importers")
        lines.append("")
        for imp in importers:
            usage_badge = " (re-export)" if imp["usage"] == "re-export" else ""
            names = ", ".join(imp["imported_names"][:5])
            lines.append(f"- `{imp['file']}` L{imp['line']}: imports `{names}`{usage_badge}")
        lines.append("")

    # Call sites
    call_sites = ctx.get("call_sites", [])
    if call_sites:
        lines.append("## Call Sites")
        lines.append("")
        for cs in call_sites[:20]:  # Cap display
            lines.append(f"- `{cs['file']}` L{cs['line']}: `{cs['code'][:80]}`")
        if len(call_sites) > 20:
            lines.append(f"- ... and {len(call_sites) - 20} more")
        lines.append("")

    # Test files
    test_files = ctx.get("test_files", [])
    if test_files:
        lines.append("## Test Coverage")
        lines.append("")
        for tf in test_files:
            lines.append(f"- `{tf['file']}` ({tf['test_count']} tests)")
        lines.append("")

    # Doc references
    doc_refs = ctx.get("doc_references", [])
    if doc_refs:
        lines.append("## Documentation References")
        lines.append("")
        for dr in doc_refs:
            sections = ", ".join(dr["sections"][:3]) if dr["sections"] else "general"
            lines.append(f"- `{dr['file']}` (sections: {sections})")
        lines.append("")

    # Infrastructure references
    infra_refs = ctx.get("infra_references", [])
    if infra_refs:
        lines.append("## Infrastructure References")
        lines.append("")
        for ir in infra_refs:
            lines.append(f"- `{ir['file']}` ({ir['label']}) \u2014 {ir['note']}")
        lines.append("")

    # Checklist
    checklist = ctx.get("checklist", [])
    if checklist:
        lines.append("## Update Checklist")
        lines.append("")
        for item in checklist:
            lines.append(f"- [ ] {item}")
        lines.append("")

    # Findings
    findings = ctx.get("findings", [])
    if findings:
        lines.append("## Findings")
        lines.append("")
        for f in findings:
            lines.append(f"- {f}")
        lines.append("")

    # Risks
    risks = ctx.get("risks", [])
    if risks:
        lines.append("## Risks")
        lines.append("")
        for r in risks:
            lines.append(f"- \u26a0\ufe0f {r}")
        lines.append("")

    return "\n".join(lines)
