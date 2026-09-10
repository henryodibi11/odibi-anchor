"""odibi_anchor.codebase._manifest — Environment Manifest loader and validator.

Provides schema validation, file loading, and auto-discovery for the
.anchor_manifest.json project manifest. The manifest gives AI agents instant
orientation: project identity, data sources, conventions, and constraints.

Dependencies: stdlib only (json, re, pathlib).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from odibi_anchor._utils.contract import (
    validate_output_format,
    build_base_context,
    finalize_context,
)
from odibi_anchor._utils.render_utils import (
    render_header_lines,
    render_metrics_lines,
    render_bullet_section,
)


# ── Constants ──────────────────────────────────────────────────────────────────────

MANIFEST_FILENAME = ".anchor_manifest.json"

VALID_PROJECT_TYPES = {"library", "pipeline", "tool", "framework", "notebook"}
VALID_REFRESH_SCHEDULES = {"daily", "hourly", "streaming", "manual"}
VALID_ACCESS_MODES = {"read", "write", "read_write"}
VALID_TOP_LEVEL_KEYS = {
    "project", "data_sources", "conventions", "dependencies",
    "tools", "constraints", "environments", "_generated", "_comment",
}


# ── Schema Validation ────────────────────────────────────────────────────────────────


def validate_manifest(data: dict) -> list[str]:
    """Validate a manifest dict against the schema rules.

    Implements all 10 validation rules from the Environment Manifest Spec.
    Returns a list of error strings. An empty list means valid.

    Args:
        data: Parsed manifest dictionary.

    Returns:
        List of validation error strings. Empty means valid.
    """
    errors: list[str] = []

    # Rule 1: Root must be an object
    if not isinstance(data, dict):
        errors.append("Root must be a JSON object (dict).")
        return errors  # Cannot validate further

    # Rule 2: project.name required
    if "project" in data:
        project = data["project"]
        if not isinstance(project, dict):
            errors.append("'project' must be an object.")
        else:
            name = project.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                errors.append("'project.name' is required and must be a non-empty string.")

            # Rule 3: project.type enum
            ptype = project.get("type")
            if ptype is not None and ptype not in VALID_PROJECT_TYPES:
                errors.append(
                    f"'project.type' must be one of {sorted(VALID_PROJECT_TYPES)}, "
                    f"got '{ptype}'."
                )

    # Rule 4: data_sources must be array of objects with name
    if "data_sources" in data:
        ds = data["data_sources"]
        if not isinstance(ds, list):
            errors.append("'data_sources' must be an array.")
        else:
            for i, entry in enumerate(ds):
                if not isinstance(entry, dict):
                    errors.append(f"'data_sources[{i}]' must be an object.")
                    continue
                ds_name = entry.get("name")
                if not ds_name or not isinstance(ds_name, str):
                    errors.append(
                        f"'data_sources[{i}].name' is required and must be a string."
                    )

                # Rule 5: refresh_schedule enum
                schedule = entry.get("refresh_schedule")
                if schedule is not None and schedule not in VALID_REFRESH_SCHEDULES:
                    errors.append(
                        f"'data_sources[{i}].refresh_schedule' must be one of "
                        f"{sorted(VALID_REFRESH_SCHEDULES)}, got '{schedule}'."
                    )

                # Rule 6: access enum
                access = entry.get("access")
                if access is not None and access not in VALID_ACCESS_MODES:
                    errors.append(
                        f"'data_sources[{i}].access' must be one of "
                        f"{sorted(VALID_ACCESS_MODES)}, got '{access}'."
                    )

    # Rule 7: constraints.sensitive_columns must be valid regexes
    if "constraints" in data:
        constraints = data["constraints"]
        if not isinstance(constraints, dict):
            errors.append("'constraints' must be an object.")
        else:
            patterns = constraints.get("sensitive_columns")
            if patterns is not None:
                if not isinstance(patterns, list):
                    errors.append("'constraints.sensitive_columns' must be an array.")
                else:
                    for i, pattern in enumerate(patterns):
                        if not isinstance(pattern, str):
                            errors.append(
                                f"'constraints.sensitive_columns[{i}]' must be a string."
                            )
                            continue
                        try:
                            re.compile(pattern)
                        except re.error as e:
                            errors.append(
                                f"'constraints.sensitive_columns[{i}]' is not a valid "
                                f"regex: '{pattern}' ({e})."
                            )

            # Rule 8: max_table_rows_local must be positive integer
            max_rows = constraints.get("max_table_rows_local")
            if max_rows is not None:
                if not isinstance(max_rows, int) or max_rows <= 0:
                    errors.append(
                        "'constraints.max_table_rows_local' must be a positive integer."
                    )

            # Also check max_file_size_kb if present
            max_size = constraints.get("max_file_size_kb")
            if max_size is not None:
                if not isinstance(max_size, int) or max_size <= 0:
                    errors.append(
                        "'constraints.max_file_size_kb' must be a positive integer."
                    )

    # Rule 9: environments values must be objects, read_only must be boolean
    if "environments" in data:
        envs = data["environments"]
        if not isinstance(envs, dict):
            errors.append("'environments' must be an object.")
        else:
            for env_name, env_val in envs.items():
                if not isinstance(env_val, dict):
                    errors.append(
                        f"'environments.{env_name}' must be an object."
                    )
                    continue
                read_only = env_val.get("read_only")
                if read_only is not None and not isinstance(read_only, bool):
                    errors.append(
                        f"'environments.{env_name}.read_only' must be a boolean."
                    )

    return errors


def _get_unknown_keys(data: dict) -> list[str]:
    """Return top-level keys not in the known schema."""
    return [k for k in data if k not in VALID_TOP_LEVEL_KEYS]


# ── Load Manifest ──────────────────────────────────────────────────────────────────────


def load_manifest(root: str | Path) -> dict:
    """Load and validate the .anchor_manifest.json from a project root.

    Reads the manifest file, parses JSON, validates against the schema.
    Returns the parsed dict on success, or an empty dict if the file is
    missing, unparseable, or invalid.

    Args:
        root: Project root directory path.

    Returns:
        Parsed manifest dict, or {} if unavailable/invalid.
    """
    root_path = Path(root)
    manifest_path = root_path / MANIFEST_FILENAME

    if not manifest_path.exists():
        return {}

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    errors = validate_manifest(data)
    if errors:
        # Attach errors as metadata — caller decides severity
        data["_validation_errors"] = errors

    return data


# ── Auto-Discovery ─────────────────────────────────────────────────────────────────────


def generate_manifest(root: str | Path) -> dict:
    """Auto-discover project metadata and generate a manifest dict.

    Scans the project root for known metadata files (pyproject.toml,
    databricks.yml, requirements.txt, test directories, etc.) and
    builds a best-effort manifest. Fields that cannot be inferred are
    omitted entirely (not set to defaults).

    Args:
        root: Project root directory path.

    Returns:
        Generated manifest dict with "_generated": True flag.
    """
    root_path = Path(root)
    manifest: dict[str, Any] = {"_generated": True}

    # ── Project section ──────────────────────────────────────────────────────
    project = _discover_project(root_path)
    if project:
        manifest["project"] = project

    # ── Dependencies section ─────────────────────────────────────────────────
    deps = _discover_dependencies(root_path)
    if deps:
        manifest["dependencies"] = deps

    # ── Conventions section ──────────────────────────────────────────────────
    conventions = _discover_conventions(root_path)
    if conventions:
        manifest["conventions"] = conventions

    # ── Tools section ────────────────────────────────────────────────────────
    tools = _discover_tools(root_path)
    if tools:
        manifest["tools"] = tools

    return manifest


def _discover_project(root: Path) -> dict[str, Any]:
    """Extract project identity from pyproject.toml or databricks.yml."""
    project: dict[str, Any] = {}

    # Try pyproject.toml (regex-based parsing for stdlib-only)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            # Extract name
            m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                project["name"] = m.group(1)
            # Extract description
            m = re.search(r'^description\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                project["description"] = m.group(1)
        except OSError:
            pass

    # Try databricks.yml
    dbx_yml = root / "databricks.yml"
    if dbx_yml.exists() and "name" not in project:
        try:
            text = dbx_yml.read_text(encoding="utf-8")
            m = re.search(r"^bundle:\s*\n\s*name:\s*(.+)", text, re.MULTILINE)
            if m:
                project["name"] = m.group(1).strip().strip("\"'")
        except OSError:
            pass

    # Fallback: use directory name
    if not project.get("name"):
        project["name"] = root.name

    # Detect language
    py_files = list(root.rglob("*.py"))
    sql_files = list(root.rglob("*.sql"))
    if py_files and sql_files:
        project["language"] = ["python", "sql"]
    elif py_files:
        project["language"] = "python"
    elif sql_files:
        project["language"] = "sql"

    return project


def _discover_dependencies(root: Path) -> dict[str, Any]:
    """Extract dependencies from requirements.txt and pyproject.toml."""
    deps: dict[str, Any] = {}

    # requirements.txt
    req_file = root / "requirements.txt"
    if req_file.exists():
        try:
            lines = req_file.read_text(encoding="utf-8").splitlines()
            external: dict[str, str] = {}
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                # Parse "package>=version" or "package==version"
                m = re.match(r"^([a-zA-Z0-9_-]+)\s*([><=!~]+.+)?$", line)
                if m:
                    pkg = m.group(1)
                    ver = m.group(2) or ""
                    external[pkg] = ver.strip()
            if external:
                deps["external"] = external
        except OSError:
            pass

    # pyproject.toml dependencies
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and "external" not in deps:
        try:
            text = pyproject.read_text(encoding="utf-8")
            # Look for dependencies array
            m = re.search(
                r"^dependencies\s*=\s*\[([^\]]+)\]",
                text,
                re.MULTILINE | re.DOTALL,
            )
            if m:
                dep_block = m.group(1)
                external = {}
                for dep_match in re.finditer(r'"([^"]+)"', dep_block):
                    dep_str = dep_match.group(1)
                    dm = re.match(r"^([a-zA-Z0-9_-]+)\s*(.*)$", dep_str)
                    if dm:
                        external[dm.group(1)] = dm.group(2).strip()
                if external:
                    deps["external"] = external
        except OSError:
            pass

    return deps


def _discover_conventions(root: Path) -> dict[str, Any]:
    """Infer coding conventions from project structure."""
    conventions: dict[str, Any] = {}

    # Test framework detection
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        conventions["testing"] = "pytest, tests/ directory, test_ prefix"

    # Import style (check a few source files)
    src_dir = root / "src"
    if src_dir.is_dir():
        py_files = list(src_dir.rglob("*.py"))[:5]
        has_absolute = False
        for f in py_files:
            try:
                text = f.read_text(encoding="utf-8")
                if re.search(r"^from [a-z_]+\.", text, re.MULTILINE):
                    has_absolute = True
                    break
            except OSError:
                continue
        if has_absolute:
            conventions["imports"] = "absolute imports, no star imports"

    return conventions


def _discover_tools(root: Path) -> list[dict[str, str]]:
    """Detect available tools from project structure."""
    tools: list[dict[str, str]] = []

    # Look for scripts/ directory
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for script in sorted(scripts_dir.glob("*.py")):
            if script.name.startswith("_"):
                continue
            tools.append({
                "name": script.stem,
                "path": str(script.relative_to(root)),
            })

    return tools



# ── Dispatcher Context ─────────────────────────────────────────────────────────────────


def manifest_context(
    root: str | Path,
    *,
    validate: bool = False,
    generate: bool = False,
    section: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Anchor dispatcher entry point for manifest operations.

    Supports viewing, validating, and generating the project manifest.
    Returns a StandardContract dict (or markdown string).

    Args:
        root: Project root directory.
        validate: If True, run validation and report errors.
        generate: If True, auto-discover and generate a manifest.
        section: If provided, show only this section of the manifest.
        output_format: "dict" or "markdown".

    Returns:
        StandardContract dict or rendered markdown string.
    """
    validate_output_format(output_format)
    root_path = Path(root)

    if generate:
        data = generate_manifest(root_path)
        errors = validate_manifest(data)
        project_name = data.get("project", {}).get("name", root_path.name)
        summary = f"Generated manifest for '{project_name}'"
    else:
        data = load_manifest(root_path)
        errors = validate_manifest(data) if (validate or data) else []
        if not data:
            summary = "No manifest found at project root"
        else:
            project_name = data.get("project", {}).get("name", "unknown")
            summary = f"Manifest loaded: {project_name}"

    # Filter to section if requested
    display_data = data
    if section and data:
        display_data = {section: data.get(section, {})}

    # Metrics
    metrics = {
        "data_source_count": len(data.get("data_sources", [])),
        "environment_count": len(data.get("environments", {})),
        "constraint_count": len(data.get("constraints", {})),
        "tool_count": len(data.get("tools", [])),
        "is_valid": len(errors) == 0,
        "is_generated": data.get("_generated", False),
        "has_manifest": bool(data),
    }

    # Findings
    findings: list[str] = []
    if errors:
        findings.extend(f"ERROR: {e}" for e in errors)
    unknown_keys = _get_unknown_keys(data) if data else []
    if unknown_keys:
        findings.append(f"WARNING: Unknown top-level keys: {unknown_keys}")

    # Suggested next actions
    actions: list[str] = []
    if not data:
        actions.append("Run anchor('manifest', generate=True) to auto-generate a manifest.")
    elif errors:
        actions.append("Fix validation errors in .anchor_manifest.json.")
    if data and not errors:
        actions.append("Manifest is valid — project is fully oriented.")

    ctx = build_base_context(
        kind="manifest",
        subject=data.get("project", {}).get("name", root_path.name),
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=[],
        samples={"manifest": display_data},
        suggested_next_actions=actions,
    )

    return finalize_context(ctx, output_format, render_manifest_report)


# ── Markdown Rendering ─────────────────────────────────────────────────────────────────


def render_manifest_report(ctx: dict) -> str:
    """Render a manifest context dict as a markdown report.

    Args:
        ctx: StandardContract dict from manifest_context().

    Returns:
        Formatted markdown string.
    """
    lines: list[str] = []

    lines.extend(render_header_lines(ctx, "Manifest"))
    lines.append("")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    # Manifest content
    manifest = ctx.get("samples", {}).get("manifest", {})
    if manifest:
        lines.append("")
        lines.append("## Manifest Content")
        lines.append("")

        # Project section
        project = manifest.get("project")
        if project:
            lines.append("### Project")
            lines.append("")
            for k, v in project.items():
                lines.append(f"- **{k}**: {v}")
            lines.append("")

        # Data sources
        ds = manifest.get("data_sources")
        if ds:
            lines.append(f"### Data Sources ({len(ds)})")
            lines.append("")
            for entry in ds:
                name = entry.get("name", "unnamed")
                catalog = entry.get("catalog", "")
                schema = entry.get("schema", "")
                access = entry.get("access", "")
                loc = f"{catalog}.{schema}" if catalog else ""
                if loc:
                    lines.append(f"- **{name}** — {loc} ({access})")
                else:
                    lines.append(f"- **{name}** ({access})")
            lines.append("")

        # Environments
        envs = manifest.get("environments")
        if envs:
            lines.append(f"### Environments ({len(envs)})")
            lines.append("")
            for env_name, env_cfg in envs.items():
                catalog = env_cfg.get("catalog", "")
                ro = " [read-only]" if env_cfg.get("read_only") else ""
                lines.append(f"- **{env_name}**: {catalog}{ro}")
            lines.append("")

        # Constraints
        constraints = manifest.get("constraints")
        if constraints:
            lines.append("### Constraints")
            lines.append("")
            for k, v in constraints.items():
                lines.append(f"- **{k}**: {v}")
            lines.append("")

    # Findings
    if ctx.get("findings"):
        lines.extend(render_bullet_section(ctx["findings"], "## Findings"))

    # Next actions
    if ctx.get("suggested_next_actions"):
        lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Next Actions"))

    return "\n".join(lines)
