"""Spec lifecycle action and gate criteria verification.

Phases 3 & 4 of SPEC_DRIVEN_WORKFLOW_SPEC.

- Phase 3: check_spec_criteria() / render_spec_criteria() used by gate
- Phase 4: spec_action() -- anchor("spec") lifecycle sub-commands
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Phase 3: Gate helper -- spec criteria verification
# ---------------------------------------------------------------------------


def check_spec_criteria(
    active_spec: dict,
    session_timings: list[dict],
    root: str,
) -> dict:
    """Check spec success_criteria against session evidence.

    Non-blocking -- informational only. Called by gate when a spec is linked.
    """
    criteria = active_spec.get("success_criteria", [])
    root_path = Path(root)

    result_criteria = []
    for criterion in criteria:
        emoji, check_type, evidence = _match_criterion(
            criterion, session_timings, root_path
        )
        result_criteria.append(
            {
                "criterion": criterion,
                "status": emoji,
                "check_type": check_type,
                "evidence": evidence,
            }
        )

    return {
        "spec_name": active_spec.get("name", "unknown"),
        "criteria": result_criteria,
    }


def render_spec_criteria(spec_criteria: dict) -> str:
    """Render spec_criteria dict as a markdown section string."""
    spec_name = spec_criteria.get("spec_name", "unknown")
    criteria = spec_criteria.get("criteria", [])
    if not criteria:
        return ""

    lines_out = [f"\n## Spec Criteria ({spec_name})\n"]
    for check in criteria:
        lines_out.append(
            f"{check['status']} {check['criterion']}  [{check['evidence']}]"
        )
    return "\n".join(lines_out)


def _match_criterion(
    criterion: str,
    session_timings: list[dict],
    root_path: Path,
) -> tuple[str, str, str]:
    """Heuristic matchers on one criterion string.

    Returns (emoji, check_type, evidence_string).

    Priority: file_exists > test_pass > anchor_action > agent_attested
    """
    # 1. File existence: criterion mentions *.py or *.md
    file_match = re.search(r'[\w./_-]+\.(?:py|md)', criterion)
    if file_match:
        candidate = file_match.group(0)
        if (root_path / candidate).exists():
            return ("✅", "file_exists", f"file: {candidate} exists")
        return ("⬜", "file_missing", f"file: {candidate} not found")

    # 2. Test pass: criterion contains "test" + "pass/passing/passes"
    c_lower = criterion.lower()
    if "test" in c_lower and any(
        w in c_lower for w in ("pass", "passing", "passes")
    ):
        test_entries = [
            t
            for t in session_timings
            if t.get("action") == "test"
            and t.get("error") is None
        ]
        if test_entries:
            return ("✅", "test_passed", "last test run passed")
        return ("⬜", "test_not_run", "no test run this session")

    # 3. anchor() action: criterion names a anchor() call
    anchor_match = re.search(r"anchor\([\"'](\w+)[\"']\)", criterion)
    if anchor_match:
        action_name = anchor_match.group(1)
        ran = any(
            t.get("action") == action_name and t.get("error") is None
            for t in session_timings
        )
        if ran:
            return ('✅', 'action_ran', f"anchor('{action_name}') ran successfully")
        return ('⬜', 'action_not_run', f"anchor('{action_name}') not yet run")

    # 4. Default: agent_attested
    return ('⬜', 'agent_attested', 'requires agent confirmation')


# ---------------------------------------------------------------------------
# Phase 4: anchor("spec") lifecycle action
# ---------------------------------------------------------------------------

_STATUS_EMOJI: dict[str, str] = {
    "done": "✅",
    "in-progress": "🔄",
    "ready": "📋",
    "draft": "📝",
    "abandoned": "🚫",
}

# Regex to update YAML frontmatter status line
_FM_STATUS_RE = re.compile(
    r'(\A---\s*\n(?:[^\n]*\n)*?)(status:\s*\S+)((?:[^\n]*\n)*?---\s*\n)',
    re.DOTALL,
)
# Regex to update bold-text **Status:** line
_BOLD_STATUS_RE = re.compile(r'(\*\*Status:\*\*\s*)(.+)')
# Detect YAML frontmatter
_HAS_FRONTMATTER_RE = re.compile(r'\A---\s*\n', re.DOTALL)

_SCAFFOLD_TEMPLATE = (
    "---\n"
    "status: draft\n"
    "complexity: null\n"
    "estimated_sessions: null\n"
    "phases: []\n"
    "success_criteria: []\n"
    "files_touched: []\n"
    "---\n\n"
    "# {title} Specification\n\n"
    "**Status:** draft  \n"
    "**Complexity:** TBD  \n"
    "**Estimated:** TBD  \n\n"
    "---\n\n"
    "## Overview\n\n"
    "[What problem does this solve?]\n\n"
    "## The Problem\n\n"
    "[Evidence from the codebase]\n\n"
    "## Design\n\n"
    "[How to fix it]\n\n"
    "## Success Criteria\n\n"
    "- [ ] [Criterion 1]\n"
    "- [ ] [Criterion 2]\n\n"
    "## Verification\n\n"
    "[How to verify it works]\n"
)


def spec_action(
    root: str | Path,
    *args: Any,
    name: str | None = None,
    specs_dir: str | Path | None = None,
    output_format: str = "markdown",
    **_extra: Any,
) -> Any:
    """anchor("spec") lifecycle action.

    Sub-commands:
        anchor("spec")                    -- list all specs with status
        anchor("spec", "create", name="X")-- scaffold new spec with frontmatter
        anchor("spec", "status", "X")     -- show detailed spec status
        anchor("spec", "done", "X")       -- mark spec as done
    """
    from odibi_anchor.codebase._spec_parser import list_specs, find_spec

    root_path = Path(root)
    _specs_dir = Path(specs_dir) if specs_dir else root_path / "specs"

    sub_command = args[0].lower().strip() if args else "list"
    sub_arg: str | None = args[1] if len(args) > 1 else None

    if sub_command in ("list", ""):
        return _spec_list(_specs_dir, output_format, list_specs)
    elif sub_command == "create":
        _name = sub_arg or name
        return _spec_create(_specs_dir, _name, output_format, root_path)
    elif sub_command == "status":
        return _spec_status(_specs_dir, sub_arg, output_format, find_spec)
    elif sub_command == "done":
        return _spec_done(_specs_dir, sub_arg, output_format, find_spec)
    elif sub_command == "persist":
        task_result = sub_arg if isinstance(sub_arg, dict) else None
        return _spec_persist(_specs_dir, task_result, output_format)
    elif sub_command == "validate":
        return _spec_validate(_specs_dir, sub_arg, root_path, output_format, find_spec)
    elif sub_command == "execute":
        return _spec_execute(_specs_dir, sub_arg, root_path, output_format, find_spec)
    elif sub_command == "review":
        return _spec_review(_specs_dir, sub_arg, root_path, output_format, find_spec)
    elif sub_command == "from_problem":
        return _spec_from_problem(
            _specs_dir,
            sub_arg,
            root_path,
            output_format,
            name=name,
            recommendation_revision=_extra.get("recommendation_revision"),
        )
    else:
        msg = (
            f"Unknown spec sub-command: '{sub_command}'. "
            "Valid: list, create, status, done, persist, validate, execute, review, from_problem"
        )
        if output_format == "dict":
            return {"error": msg}
        return msg


def _spec_list(specs_dir: Path, output_format: str, list_specs_fn: Any) -> Any:
    """List all specs with emoji status."""
    specs = list_specs_fn(specs_dir)
    if not specs:
        msg = f"No specs found in {specs_dir}"
        if output_format == "dict":
            return {"specs": [], "total": 0, "summary": msg}
        return msg

    lines_out = [f"Specs ({len(specs)} total):\n"]
    for s in specs:
        emoji = _STATUS_EMOJI.get(s["status"], "❓")
        lines_out.append(f"  {emoji} {s['status']:<14} {s['name']}")

    result_dict = {
        "specs": specs,
        "total": len(specs),
        "summary": "\n".join(lines_out),
    }
    if output_format == "dict":
        return result_dict
    return result_dict["summary"]


def _spec_status(
    specs_dir: Path, query: str | None, output_format: str, find_spec_fn: Any
) -> Any:
    """Show detailed spec status."""
    if not query:
        return 'Usage: anchor("spec", "status", "NAME_FRAGMENT")'
    spec = find_spec_fn(specs_dir, query)
    if spec is None:
        msg = f"Spec not found: '{query}'"
        if output_format == "dict":
            return {"error": msg}
        return msg
    emoji = _STATUS_EMOJI.get(spec["status"], "❓")
    out = [
        f"## {emoji} {spec['name']}\n",
        f"**Status:** {spec['status']}",
        f"**Complexity:** {spec['complexity'] or 'unknown'}",
        f"**Estimated sessions:** {spec['estimated_sessions'] or 'unknown'}",
        f"**File:** {spec['raw_path']}\n",
    ]
    if spec.get("phases"):
        out.append("### Phases")
        for p in spec["phases"]:
            p_emoji = _STATUS_EMOJI.get(p.get("status", "draft"), "❓")
            out.append(f"  {p_emoji} {p.get('name', '?')} -- {p.get('status', '?')}")
        out.append("")
    if spec.get("success_criteria"):
        out.append("### Success Criteria")
        for c in spec["success_criteria"]:
            out.append(f"  - {c}")
    if output_format == "dict":
        return spec
    return "\n".join(out)


def _spec_create(
    specs_dir: Path, name: str | None, output_format: str,
    root_path: "Path | str | None" = None,
) -> Any:
    """Scaffold a new spec file with YAML frontmatter."""
    if not name:
        msg = 'Usage: anchor("spec", "create", name="FEATURE_NAME")'
        if output_format == "dict":
            return {"error": msg}
        return msg
    clean_name = name.upper().replace(" ", "_").replace("-", "_")
    spec_path = specs_dir / f"{clean_name}_SPEC.md"
    if spec_path.exists():
        msg = f"Spec already exists: {spec_path}"
        if output_format == "dict":
            return {"error": msg, "path": str(spec_path)}
        return msg
    specs_dir.mkdir(parents=True, exist_ok=True)
    title = clean_name.replace("_", " ").title()
    spec_path.write_text(_SCAFFOLD_TEMPLATE.format(title=title), encoding="utf-8")
    msg = f"Created: {spec_path}\nEdit the file to fill in details."
    if output_format == "dict":
        return {
            "created": True, "path": str(spec_path), "name": clean_name,
            "summary": msg,
            "write_performed": True, "artifact_path": str(spec_path),
        }
    return msg


def _spec_done(
    specs_dir: Path, query: str | None, output_format: str, find_spec_fn: Any
) -> Any:
    """Mark a spec as done: update YAML frontmatter and bold-text status."""
    if not query:
        msg = 'Usage: anchor("spec", "done", "NAME_FRAGMENT")'
        if output_format == "dict":
            return {"error": msg}
        return msg
    spec = find_spec_fn(specs_dir, query)
    if spec is None:
        msg = f"Spec not found: '{query}'"
        if output_format == "dict":
            return {"error": msg}
        return msg
    spec_path = Path(spec["raw_path"])
    text = spec_path.read_text(encoding="utf-8")
    # Update YAML frontmatter status field (if present)
    if _HAS_FRONTMATTER_RE.match(text):
        text = _FM_STATUS_RE.sub(
            lambda m: m.group(1) + "status: done" + m.group(3),
            text,
            count=1,
        )
    # Update bold-text **Status:** line
    text = _BOLD_STATUS_RE.sub('\\1✅ Done', text, count=1)
    spec_path.write_text(text, encoding="utf-8")  # explicit encoding for Windows
    msg = f"✅ Marked done: {spec['name']}_SPEC.md"
    if output_format == "dict":
        return {"updated": True, "spec_name": spec["name"], "path": str(spec_path), "summary": msg,
                "write_performed": True, "artifact_path": str(spec_path)}
    return msg


# ---------------------------------------------------------------------------
# Phase 2: Persist -- convert task_result → spec file
# ---------------------------------------------------------------------------


def _sanitize_spec_name(goal: str, *, max_length: int = 50) -> str:
    """Convert goal text to a deterministic spec file name.

    Rules: uppercase, spaces -> underscores, strip non-alphanumeric/underscore,
    truncate to *max_length* characters (default 50) on a word boundary.
    """
    name = goal.strip().upper().replace(" ", "_").replace("-", "_")
    name = re.sub(r"[^A-Z0-9_]", "", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return "UNNAMED_SPEC"
    if len(name) > max_length:
        # Truncate on underscore boundary to avoid mid-word cuts
        truncated = name[:max_length]
        last_sep = truncated.rfind("_")
        if last_sep > max_length // 2:
            truncated = truncated[:last_sep]
        name = truncated.rstrip("_")
    return name or "UNNAMED_SPEC"


def canonical_spec_name(value: str) -> str:
    """Normalize a spec query, path, or result name to one canonical key."""
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    lowered = name.lower()
    for extension in (".markdown", ".md"):
        if lowered.endswith(extension):
            name = name[:-len(extension)]
            break
    if name.upper().endswith("_SPEC"):
        name = name[:-5]
    return _sanitize_spec_name(name)


def _spec_from_problem(
    specs_dir: Path,
    problem_id: str | None,
    _root_path: Path,
    output_format: str,
    *,
    name: str | None = None,
    recommendation_revision: int | None = None,
) -> Any:
    """Create an implementation contract from an accepted recommendation."""
    if not problem_id:
        msg = 'Usage: anchor("spec", "from_problem", "PRB-YYYY-NNNN", name="FEATURE")'
        return {"error": msg} if output_format == "dict" else msg

    from odibi_anchor._dispatcher._problem import (
        _parse,
        _problem_path,
        _validate_recommendation_traceability,
        problem_action,
    )

    artifact_root = specs_dir.resolve().parent
    problem_path = _problem_path(artifact_root, problem_id)
    if not problem_path.is_file():
        raise FileNotFoundError(f"Problem Record does not exist: {problem_id}")
    record = _parse(problem_path)
    if not record["recommendation"]:
        raise ValueError("from_problem requires a captured recommendation")
    linked_evidence_ids = _validate_recommendation_traceability(record)

    spec_name = _sanitize_spec_name(name or record["meta"]["title"])
    spec_path = specs_dir / f"{spec_name}_SPEC.md"
    if spec_path.exists():
        raise FileExistsError(f"Spec already exists: {spec_path}")
    linked_revision = int(record["meta"].get("revision", 1))
    if recommendation_revision is not None and (
        type(recommendation_revision) is not int or recommendation_revision != linked_revision
    ):
        raise ValueError("recommendation_revision must be the current integer revision")
    evidence_by_id = {row["ID"]: row for row in record["evidence"]}
    source_lines = "\n".join(
        f"- `{evidence_id}` — {evidence_by_id[evidence_id].get('Source', '[source not captured]')}"
        for evidence_id in linked_evidence_ids
    )
    title = spec_name.replace("_", " ").title()
    content = (
        "---\n"
        "status: draft\n"
        "complexity: null\n"
        "estimated_sessions: null\n"
        f"problem_id: {record['meta']['problem_id']}\n"
        f"recommendation_revision: {linked_revision}\n"
        f"evidence_ids: [{', '.join(linked_evidence_ids)}]\n"
        "phases: []\n"
        "success_criteria: []\n"
        "files_touched: []\n"
        "---\n\n"
        f"# {title} Specification\n\n"
        f"**Originating problem:** `{record['meta']['problem_id']}`  \n"
        f"**Recommendation revision:** {linked_revision}\n\n"
        "## Overview\n\n"
        f"{record['recommendation']}\n\n"
        "## Problem Context\n\n"
        f"{record['definition'] or record['meta']['title']}\n\n"
        "## Supporting Evidence\n\n"
        f"{source_lines}\n\n"
        "## Design\n\n[Translate the accepted recommendation into implementation details.]\n\n"
        "## Success Criteria\n\n- [ ] [Add testable implementation criterion]\n\n"
        "## Verification\n\n[How to verify the implementation and its intended outcome.]\n"
    )
    specs_dir.mkdir(parents=True, exist_ok=True)
    temporary = spec_path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(spec_path)
    link_result = problem_action(
        artifact_root,
        "link_spec",
        problem_id,
        spec=spec_name,
        recommendation_revision=linked_revision,
        output_format="dict",
    )
    result = {
        "created": True,
        "path": str(spec_path),
        "write_performed": True,
        "artifact_path": str(spec_path),
        "name": spec_name,
        "problem_id": record["meta"]["problem_id"],
        "recommendation_revision": linked_revision,
        "summary": f"Created {spec_name} from {problem_id} recommendation revision {linked_revision}.",
        "managed_writes": [{"path": str(spec_path), "kind": "spec"}] + (
            [{"path": str(problem_path), "kind": "problem"}]
            if link_result.get("write_performed") else []
        ),
    }
    return result if output_format == "dict" else result["summary"]


def render_spec_result(selector: str, result: dict[str, Any]) -> str:
    """Transport-neutral renderer preserving each spec selector's Markdown."""
    if result.get("error"):
        return str(result["error"])
    if selector == "status" and "raw_path" in result:
        emoji = _STATUS_EMOJI.get(result["status"], "❓")
        out = [f"## {emoji} {result['name']}\n", f"**Status:** {result['status']}",
               f"**Complexity:** {result['complexity'] or 'unknown'}",
               f"**Estimated sessions:** {result['estimated_sessions'] or 'unknown'}",
               f"**File:** {result['raw_path']}\n"]
        if result.get("phases"):
            out.append("### Phases")
            for phase in result["phases"]:
                out.append(f"  {_STATUS_EMOJI.get(phase.get('status', 'draft'), '❓')} {phase.get('name', '?')} -- {phase.get('status', '?')}")
            out.append("")
        if result.get("success_criteria"):
            out.append("### Success Criteria")
            out.extend(f"  - {criterion}" for criterion in result["success_criteria"])
        return "\n".join(out)
    if selector == "validate" and "checks" in result:
        lines = [f"## Spec Validation: {result['spec_name']}", "",
                 f"**Valid:** {'✅ Yes' if result.get('valid') else '❌ No'}", "", "### Checks", ""]
        lines.extend(f"  {check['emoji']} **{check['name']}** — {check['detail']}" for check in result["checks"])
        actions = result.get("suggested_next_actions", [])
        if actions:
            lines.extend(["", "### Suggested Actions", ""])
            lines.extend(f"  - {action}" for action in actions)
        return "\n".join(lines)
    if selector == "execute" and result.get("executing"):
        files = result.get("files_touched", [])
        phases = result.get("phases", [])
        lines = [f"## Executing: {result['spec_name']}", "", "**Status:** executing",
                 f"**Phases:** {len(phases)}",
                 f"**Files:** {', '.join(files[:5])}{'...' if len(files) > 5 else ''}",
                 f"**Validation:** {result['validation_summary']}", "", "### Execution Plan", ""]
        for phase in phases:
            lines.append(f"#### Phase {phase['number']}: {phase['name']}")
            if phase["files"]:
                lines.append(f"  Files: {', '.join(phase['files'])}")
            lines.append(f"  Tools: {', '.join(phase['tools'])}")
            lines.extend(f"  {index}. {step}" for index, step in enumerate(phase["suggested_steps"], 1))
            lines.append("")
        if result.get("acceptance_criteria"):
            lines.extend(["### Acceptance Criteria", ""])
            lines.extend(f"  - [ ] {item}" for item in result["acceptance_criteria"])
        return "\n".join(lines)
    return str(result.get("summary") or result)


def _spec_goal_text(task_result: dict) -> str:
    """Resolve the canonical task goal to use for persisted spec naming.

    Prefer the explicit user goal captured in the task result over generated
    planner summaries such as "Prepare implementation work for ...".
    """
    intent = task_result.get("intent", {})
    if not isinstance(intent, dict):
        intent = {}

    return (
        task_result.get("goal")
        or intent.get("goal")
        or intent.get("task")
        or task_result.get("subject")
        or task_result.get("summary")
        or "Untitled"
    )


def _spec_from_task(task_result: dict) -> str:
    """Convert a task_result dict into a spec markdown string with YAML frontmatter.

    Handles missing fields gracefully — any missing section is omitted or uses
    a placeholder. The output matches the scaffold template structure.
    """
    from datetime import datetime, timezone

    intent = task_result.get("intent", {})
    goal = (
        task_result.get("goal")
        or (intent.get("goal") if isinstance(intent, dict) else None)
        or task_result.get("summary")
        or task_result.get("subject")
        or "Untitled"
    )
    mode = task_result.get("mode", "implementation")
    readiness = task_result.get("readiness", {})
    score = readiness.get("score", 0) if isinstance(readiness, dict) else 0

    def _as_string(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            preferred_keys = (
                "name",
                "summary",
                "action",
                "goal",
                "task",
                "current_state",
                "trigger",
                "phase",
                "why",
                "done_when",
                "path",
                "skill",
            )
            parts = [
                str(value[key]).strip()
                for key in preferred_keys
                if value.get(key) not in (None, "", [], {})
            ]
            if parts:
                return " | ".join(parts)
            return " | ".join(
                str(item).strip()
                for item in value.values()
                if item not in (None, "", [], {})
            )
        if isinstance(value, (list, tuple, set)):
            parts = []
            for item in value:
                rendered = _as_string(item)
                if rendered:
                    parts.append(rendered)
            return ", ".join(parts)
        return str(value)

    def _as_string_list(value: Any) -> list[str]:
        if value in (None, "", [], {}):
            return []
        items = value if isinstance(value, list) else [value]
        rendered_items: list[str] = []
        for item in items:
            rendered = _as_string(item)
            if rendered:
                rendered_items.append(rendered)
        return rendered_items

    def _as_path_list(value: Any) -> list[str]:
        if value in (None, "", [], {}):
            return []
        items = value if isinstance(value, list) else [value]
        rendered_paths: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("path"):
                rendered = _as_string(item.get("path"))
            else:
                rendered = _as_string(item)
            if rendered:
                rendered_paths.append(rendered)
        return rendered_paths

    verification = task_result.get("verification", {})
    verification = verification if isinstance(verification, dict) else {}
    scope = task_result.get("scope", {})
    scope = scope if isinstance(scope, dict) else {}
    resources = task_result.get("resources", {})
    resources = resources if isinstance(resources, dict) else {}

    # Extract sections from task_result, supporting both legacy flat keys and current nested schema.
    acceptance_criteria = _as_string_list(
        task_result.get("acceptance_criteria")
        or verification.get("acceptance_criteria")
    )
    in_scope = _as_string_list(task_result.get("in_scope") or scope.get("in_scope"))
    out_of_scope = _as_string_list(
        task_result.get("out_of_scope") or scope.get("out_of_scope")
    )
    constraints = _as_string_list(task_result.get("constraints"))
    risks = _as_string_list(task_result.get("risks"))
    background = _as_string(task_result.get("background") or "")
    deliverables = _as_string_list(
        task_result.get("deliverables") or verification.get("deliverables")
    )
    action_plan = _as_string_list(task_result.get("action_plan") or task_result.get("plan"))
    artifacts = _as_path_list(task_result.get("artifacts") or resources.get("artifacts"))

    # Build phases from action_plan if available
    phases_yaml: list[str] = []
    if action_plan:
        for step_name in action_plan:
            phases_yaml.append(f'  - name: "{step_name}"')
            phases_yaml.append("    status: draft")

    # Build success_criteria YAML
    criteria_yaml: list[str] = []
    for ac in acceptance_criteria:
        escaped = ac.replace('"', '\\"')
        criteria_yaml.append(f'  - "{escaped}"')

    # Build files_touched from artifacts
    files_yaml: list[str] = []
    for path in artifacts:
        if path:
            files_yaml.append(f'  - "{path}"')

    # YAML frontmatter
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm_lines = [
        "---",
        "status: draft",
        f"mode: {mode}",
        f"readiness_score: {score}",
        f"created: {now_iso}",
    ]
    if phases_yaml:
        fm_lines.append("phases:")
        fm_lines.extend(phases_yaml)
    else:
        fm_lines.append("phases: []")
    if criteria_yaml:
        fm_lines.append("success_criteria:")
        fm_lines.extend(criteria_yaml)
    else:
        fm_lines.append("success_criteria: []")
    if files_yaml:
        fm_lines.append("files_touched:")
        fm_lines.extend(files_yaml)
    else:
        fm_lines.append("files_touched: []")
    fm_lines.append("permissions_needed: []")
    fm_lines.append("---")

    # Markdown body
    title = _sanitize_spec_name(goal).replace("_", " ").title()
    body_lines = [
        "",
        f"# {title}",
        "",
        "## Problem",
        "",
        background if background else "[Describe the problem]",
        "",
        "## Design",
        "",
    ]

    # Scope
    if in_scope or out_of_scope:
        body_lines.append("### Scope")
        body_lines.append("")
        if in_scope:
            body_lines.append("**In scope:**")
            for item in in_scope:
                body_lines.append(f"- {item}")
            body_lines.append("")
        if out_of_scope:
            body_lines.append("**Out of scope:**")
            for item in out_of_scope:
                body_lines.append(f"- {item}")
            body_lines.append("")

    # Phases
    if action_plan:
        body_lines.append("## Phases")
        body_lines.append("")
        for i, step in enumerate(action_plan, 1):
            step_name = step if isinstance(step, str) else step.get("name", str(step))
            body_lines.append(f"{i}. {step_name}")
        body_lines.append("")

    # Acceptance Criteria
    if acceptance_criteria:
        body_lines.append("## Acceptance Criteria")
        body_lines.append("")
        for ac in acceptance_criteria:
            body_lines.append(f"- [ ] {ac}")
        body_lines.append("")

    # Standards / Constraints
    if constraints:
        body_lines.append("## Standards Compliance")
        body_lines.append("")
        for c in constraints:
            body_lines.append(f"- {c}")
        body_lines.append("")

    # Risks
    if risks:
        body_lines.append("## Risks")
        body_lines.append("")
        for r in risks:
            body_lines.append(f"- {r}")
        body_lines.append("")

    # Deliverables
    if deliverables:
        body_lines.append("## Deliverables")
        body_lines.append("")
        for d in deliverables:
            body_lines.append(f"- {d}")
        body_lines.append("")

    # Verification
    body_lines.append("## Verification")
    body_lines.append("")
    body_lines.append("[How to verify this spec is complete]")
    body_lines.append("")

    return "\n".join(fm_lines + body_lines)


def _spec_persist(
    specs_dir: Path, task_result: dict | None, output_format: str
) -> Any:
    """Persist a task_result as a spec file.

    Usage: anchor("spec", "persist", task_result_dict)
    """
    if not task_result or not isinstance(task_result, dict):
        msg = 'Usage: anchor("spec", "persist", task_result_dict)'
        if output_format == "dict":
            return {"error": msg}
        return msg

    goal = task_result.get("goal") or task_result.get("summary") or ""
    if not goal:
        msg = "Cannot persist spec: task_result has no 'goal' or 'summary'."
        if output_format == "dict":
            return {"error": msg}
        return msg

    clean_name = _sanitize_spec_name(goal)
    spec_path = specs_dir / f"{clean_name}_SPEC.md"

    if spec_path.exists():
        msg = f"Spec already exists: {spec_path}\nUse a different goal or remove the existing spec."
        if output_format == "dict":
            return {"error": msg, "path": str(spec_path), "name": clean_name}
        return msg

    # Generate and write spec content
    specs_dir.mkdir(parents=True, exist_ok=True)
    content = _spec_from_task(task_result)
    spec_path.write_text(content, encoding="utf-8")

    # Always return dict so post-dispatch can detect success and set
    # spec_persisted=True regardless of output_format.
    return {
        "created": True,
        "path": str(spec_path),
        "write_performed": True,
        "artifact_path": str(spec_path),
        "name": clean_name,
        "summary": f"Persisted spec: {spec_path.name}\n   Path: {spec_path}",
    }


# ---------------------------------------------------------------------------
# Phase 4: Validate -- check spec freshness against codebase
# ---------------------------------------------------------------------------


def _spec_validate(
    specs_dir: Path,
    query: str | None,
    project_root: Path,
    output_format: str,
    find_spec_fn: Any,
) -> Any:
    """Validate a spec's assumptions against the current codebase.

    Checks 5 staleness dimensions:
    1. files_exist -- all files_touched still exist
    2. files_unchanged -- compare hashes to manifest
    3. imports_valid -- .py imports resolve
    4. dependencies_available -- listed deps are installed
    5. spec_age -- warn if > 14 days old
    """
    import hashlib
    import json
    import importlib.util
    from datetime import datetime, timezone

    if not query:
        msg = 'Usage: anchor("spec", "validate", "FEATURE_NAME")'
        if output_format == "dict":
            return {"error": msg}
        return msg

    spec = find_spec_fn(specs_dir, query)
    if spec is None:
        msg = f"Spec not found: '{query}'"
        if output_format == "dict":
            return {"error": msg}
        return msg

    spec_name = spec["name"]
    spec_path = Path(spec["raw_path"])
    files_touched = spec.get("files_touched", [])

    # Read frontmatter for additional fields
    spec_text = spec_path.read_text(encoding="utf-8")
    from odibi_anchor.codebase._spec_parser import _parse_frontmatter
    fm = _parse_frontmatter(spec_text) or {}
    created_str = fm.get("created", "")
    dependencies = fm.get("dependencies", [])

    checks: list[dict] = []
    stale_files: list[dict] = []
    broken_imports: list[dict] = []
    missing_deps: list[dict] = []

    # ---- Check 1: Files exist ----
    missing_files = []
    for f in files_touched:
        full_path = project_root / f
        if not full_path.exists():
            missing_files.append(f)

    checks.append({
        "name": "files_exist",
        "passed": len(missing_files) == 0,
        "emoji": "✅" if not missing_files else "❌",
        "detail": f"{len(files_touched) - len(missing_files)}/{len(files_touched)} files exist",
        "missing": missing_files,
    })

    # ---- Check 2: Files unchanged since the spec was written ----
    # No per-file hash manifest exists, so use the spec file's own mtime as the
    # reference point: a touched file modified AFTER the spec was last written
    # is "stale" — the spec may no longer reflect its current state. Comparing
    # mtimes (sub-second, untruncated) reliably separates files written before
    # the spec from those changed after it.
    changed_files: list[dict] = []
    try:
        spec_mtime = spec_path.stat().st_mtime
    except OSError:
        spec_mtime = None
    if spec_mtime is not None:
        for f in files_touched:
            full_path = project_root / f
            if not full_path.exists():
                continue  # absence is reported by the files_exist check
            try:
                if full_path.stat().st_mtime > spec_mtime:
                    changed_files.append({"file": f})
            except OSError:
                continue
    stale_files = changed_files

    checks.append({
        "name": "files_unchanged",
        "passed": len(changed_files) == 0,
        "emoji": "✅" if not changed_files else "⚠️",
        "detail": (
            f"{len(changed_files)} file(s) changed since spec written"
            if changed_files else "No files changed since spec written"
        ),
        "changed": changed_files,
    })

    # ---- Check 3: Imports valid ----
    checks.append({
        "name": "imports_valid",
        "passed": True,
        "emoji": "✅",
        "detail": "Import validation skipped",
        "broken": broken_imports,
    })

    # ---- Check 4: Dependencies available ----
    for dep in dependencies:
        # Parse "package>=version" format
        dep_name = re.split(r"[><=!]", dep)[0].strip()
        dep_spec = dep.strip()
        if dep_name and importlib.util.find_spec(dep_name.replace("-", "_")) is None:
            missing_deps.append({"package": dep_name, "spec": dep_spec})

    checks.append({
        "name": "dependencies_available",
        "passed": len(missing_deps) == 0,
        "emoji": "✅" if not missing_deps else "⚠️",
        "detail": f"{len(missing_deps)} missing dep(s)" if missing_deps else "All dependencies installed",
        "missing": missing_deps,
    })

    # ---- Check 5: Spec age ----
    age_days: int | None = None
    age_warning = False
    if created_str:
        try:
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created_dt).days
            age_warning = age_days > 14
        except (ValueError, TypeError):
            pass

    checks.append({
        "name": "spec_age",
        "passed": not age_warning,
        "emoji": "✅" if not age_warning else "⚠️",
        "detail": f"{age_days} days old" if age_days is not None else "Age unknown (no created timestamp)",
        "age_days": age_days,
    })

    # ---- Overall result ----
    all_passed = all(c["passed"] for c in checks)
    has_errors = any(c["emoji"] == "❌" for c in checks)

    suggested_actions: list[str] = []
    if missing_files:
        suggested_actions.append('Files were deleted — update spec files_touched or recreate them')
    if changed_files:
        suggested_actions.append('Files changed since spec was written — update spec or re-validate')
    if broken_imports:
        suggested_actions.append('Fix broken imports or install missing packages')
    if missing_deps:
        suggested_actions.append(f'Install missing: pip install {" ".join(d["spec"] for d in missing_deps)}')
    if age_warning:
        suggested_actions.append('Spec is >14 days old — review for staleness and re-validate')
    if all_passed:
        suggested_actions.append('All checks pass — spec is ready for execution')

    result_dict = {
        "spec_name": spec_name,
        "valid": all_passed,
        "has_errors": has_errors,
        "checks": checks,
        "stale_files": stale_files,
        "broken_imports": broken_imports,
        "missing_deps": missing_deps,
        "suggested_next_actions": suggested_actions,
    }

    if output_format == "dict":
        return result_dict

    # Markdown output
    status_text = '\u2705 Yes' if all_passed else '\u274c No'
    lines = [
        f"## Spec Validation: {spec_name}",
        "",
        f"**Valid:** {status_text}",
        "",
        "### Checks",
        "",
    ]
    for c in checks:
        lines.append(f"  {c['emoji']} **{c['name']}** — {c['detail']}")
    if suggested_actions:
        lines.append("")
        lines.append("### Suggested Actions")
        lines.append("")
        for a in suggested_actions:
            lines.append(f"  - {a}")
    return "\n".join(lines)


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _can_resolve_import(module_name: str) -> bool:
    """Check if a top-level module can be resolved."""
    import importlib.util
    try:
        # Only check top-level package
        top_level = module_name.split(".")[0]
        return importlib.util.find_spec(top_level) is not None
    except (ModuleNotFoundError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Phase 5: Execute -- begin spec-driven execution
# ---------------------------------------------------------------------------


def _spec_execute(
    specs_dir: Path,
    query: str | None,
    project_root: Path,
    output_format: str,
    find_spec_fn: Any,
) -> Any:
    """Begin execution of a validated spec.

    Workflow:
    1. Find and validate the spec (block if stale)
    2. Update spec status to 'executing'
    3. Return execution plan with phases, files, tools, acceptance criteria
    4. Result includes active_spec dict for session state linkage
    """
    if not query:
        msg = 'Usage: anchor("spec", "execute", "FEATURE_NAME")'
        if output_format == "dict":
            return {"error": msg}
        return msg

    spec = find_spec_fn(specs_dir, query)
    if spec is None:
        msg = f"Spec not found: '{query}'"
        if output_format == "dict":
            return {"error": msg}
        return msg

    # Block if already done
    if spec.get("status") == "done":
        msg = f"Spec '{spec['name']}' is already done. Cannot re-execute."
        if output_format == "dict":
            return {"error": msg}
        return msg

    # ---- Step 1: Validate ----
    validation = _spec_validate(
        specs_dir, query, project_root, "dict", find_spec_fn
    )
    if isinstance(validation, dict) and validation.get("has_errors"):
        msg = (
            f"Spec '{spec['name']}' has validation errors — cannot execute.\n"
            f"Run anchor('spec', 'validate', '{query}') for details.\n"
            f"Fix issues first, then retry."
        )
        if output_format == "dict":
            return {
                "error": msg,
                "validation": validation,
                "spec_name": spec["name"],
            }
        return msg

    # ---- Step 2: Update spec status to 'executing' ----
    spec_path = Path(spec["raw_path"])
    _update_spec_status(spec_path, "executing")

    # ---- Step 3: Build execution plan ----
    phases = spec.get("phases", [])
    files_touched = spec.get("files_touched", [])
    success_criteria = spec.get("success_criteria", [])

    # Build phase plan with suggested tools
    execution_phases: list[dict] = []
    for i, phase in enumerate(phases, 1):
        phase_name = phase.get("name", f"Phase {i}") if isinstance(phase, dict) else str(phase)
        phase_status = phase.get("status", "draft") if isinstance(phase, dict) else "draft"
        phase_files = phase.get("files", []) if isinstance(phase, dict) else []
        phase_tools = phase.get("tools", []) if isinstance(phase, dict) else []

        execution_phases.append({
            "number": i,
            "name": phase_name,
            "status": phase_status,
            "files": phase_files,
            "tools": phase_tools or ["anchor('safe')", "anchor('touched')", "anchor('test')"],
            "suggested_steps": [
                f"Implement phase {i}",
                "Run anchor('touched', 'file') for each changed file",
                "Run anchor('test', target='relevant_tests')",
                f"Run anchor('checkpoint', label='phase_{i}')",
            ],
        })

    # If no phases defined, create a single default phase
    if not execution_phases:
        execution_phases.append({
            "number": 1,
            "name": "Implementation",
            "status": "draft",
            "files": files_touched,
            "tools": ["anchor('safe')", "anchor('touched')", "anchor('test')", "anchor('gate')"],
            "suggested_steps": [
                "Apply changes to project files",
                "Run anchor('touched', 'file') for each changed file",
                "Run anchor('test') to verify",
                "Run anchor('gate') to finalize",
            ],
        })

    # Build active_spec dict for session linkage
    active_spec = {
        "name": spec["name"],
        "path": str(spec_path),
        "status": "executing",
        "phases": execution_phases,
        "files_touched": files_touched,
        "success_criteria": success_criteria,
        "validation": validation,
    }

    result_dict = {
        "executing": True,
        "write_performed": True,
        "artifact_path": str(spec_path),
        "spec_name": spec["name"],
        "spec_path": str(spec_path),
        "active_spec": active_spec,
        "phases": execution_phases,
        "total_phases": len(execution_phases),
        "files_touched": files_touched,
        "acceptance_criteria": success_criteria,
        "validation_summary": f"{sum(1 for c in validation.get('checks', []) if c.get('passed'))}/{len(validation.get('checks', []))} checks passed",
        "suggested_next_actions": [
            f"Follow {len(execution_phases)} phase(s) in order",
            "Apply changes to project using anchor('safe')",
            "Run anchor('checkpoint', label='phase_N') between phases",
            "Run anchor('gate') after all phases complete",
            f"Run anchor('spec', 'done', '{spec['name']}') when finished",
        ],
    }

    if output_format == "dict":
        return result_dict

    # Markdown output
    lines = [
        f"## Executing: {spec['name']}",
        "",
        f"**Status:** executing",
        f"**Phases:** {len(execution_phases)}",
        f"**Files:** {', '.join(files_touched[:5])}{'...' if len(files_touched) > 5 else ''}",
        f"**Validation:** {result_dict['validation_summary']}",
        "",
        "### Execution Plan",
        "",
    ]
    for phase in execution_phases:
        lines.append(f"#### Phase {phase['number']}: {phase['name']}")
        if phase["files"]:
            lines.append(f"  Files: {', '.join(phase['files'])}")
        lines.append(f"  Tools: {', '.join(phase['tools'])}")
        for step_idx, step in enumerate(phase["suggested_steps"], 1):
            lines.append(f"  {step_idx}. {step}")
        lines.append("")

    if success_criteria:
        lines.append("### Acceptance Criteria")
        lines.append("")
        for ac in success_criteria:
            lines.append(f"  - [ ] {ac}")

    return "\n".join(lines)


def _update_spec_status(spec_path: Path, new_status: str) -> None:
    """Update the status field in a spec file's YAML frontmatter and bold text."""
    text = spec_path.read_text(encoding="utf-8")
    # Update YAML frontmatter status
    if _HAS_FRONTMATTER_RE.match(text):
        text = _FM_STATUS_RE.sub(
            lambda m: m.group(1) + f"status: {new_status}" + m.group(3),
            text,
            count=1,
        )
    # Update bold-text **Status:** line
    status_display = new_status.replace("-", " ").title()
    text = _BOLD_STATUS_RE.sub(f'\\1{status_display}', text, count=1)
    spec_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 6: Review -- audit spec against standards
# ---------------------------------------------------------------------------


_REQUIRED_SECTIONS = frozenset({
    "problem", "design", "acceptance criteria", "verification",
})


def _has_docstring(source_text: str) -> bool:
    """Check if source text contains a docstring."""
    tq1 = chr(34) * 3  # triple double-quote
    tq2 = chr(39) * 3  # triple single-quote
    return tq1 in source_text or tq2 in source_text


def _acceptance_block(spec_text_lower: str) -> str:
    """Extract the Acceptance Criteria section body (lowercased), or '' if absent."""
    marker = "## acceptance criteria"
    idx = spec_text_lower.find(marker)
    if idx == -1:
        return ""
    rest = spec_text_lower[idx + len(marker):]
    nxt = rest.find("\n## ")
    return rest[:nxt] if nxt != -1 else rest


def _spec_review(
    specs_dir: Path,
    query: str | None,
    project_root: Path,
    output_format: str,
    find_spec_fn: Any,
) -> Any:
    """Review a spec against quality standards (7 checks)."""
    from datetime import datetime, timezone

    if not query:
        msg = 'Usage: anchor("spec", "review", "FEATURE_NAME")'
        if output_format == "dict":
            return {"error": msg}
        return msg

    spec = find_spec_fn(specs_dir, query)
    if spec is None:
        msg = f"Spec not found: '{query}'"
        if output_format == "dict":
            return {"error": msg}
        return msg

    spec_name = spec["name"]
    spec_path = Path(spec["raw_path"])
    spec_text = spec_path.read_text(encoding="utf-8")
    spec_lower = spec_text.lower()

    from odibi_anchor.codebase._spec_parser import _parse_frontmatter
    fm = _parse_frontmatter(spec_text) or {}
    mode = fm.get("mode", "implementation")
    files_touched = spec.get("files_touched", []) or fm.get("files_touched", [])
    permissions = fm.get("permissions_needed", [])

    checks: list[dict] = []
    suggested_improvements: list[str] = []

    # ---- Check 1: Completeness ----
    missing_sections = []
    for section in _REQUIRED_SECTIONS:
        if f"## {section}" not in spec_lower:
            missing_sections.append(section.title())

    checks.append({
        "name": "completeness",
        "passed": len(missing_sections) == 0,
        "emoji": "\u2705" if not missing_sections else "\u274c",
        "detail": "All required sections present" if not missing_sections else f"Missing: {', '.join(missing_sections)}",
        "missing": missing_sections,
    })
    if missing_sections:
        suggested_improvements.append(f"Add missing sections: {', '.join(missing_sections)}")

    # ---- Check 2: Acceptance criteria are testable (EARS-style) (S-3) ----
    ac_block = _acceptance_block(spec_lower)
    _testable_markers = (
        "shall", "must", "when ", "==", "<=", ">=", "%", " rows",
        "return", "error", "within", "at least", "no more than",
    )
    ac_testable = bool(ac_block.strip()) and (
        any(mk in ac_block for mk in _testable_markers)
        or any(ch.isdigit() for ch in ac_block)
    )
    checks.append({
        "name": "standards_alignment",
        "passed": ac_testable,
        "emoji": "\u2705" if ac_testable else "\u274c",
        "detail": (
            "Acceptance criteria are testable"
            if ac_testable
            else "Acceptance criteria missing or vague \u2014 use measurable/EARS form "
                 "('WHEN <trigger>, the system SHALL <behavior>')"
        ),
    })
    if not ac_testable:
        suggested_improvements.append(
            "Make acceptance criteria testable (EARS: 'WHEN <trigger>, the system "
            "SHALL <behavior>') with measurable values"
        )

    # ---- Check 3: Test / verification coverage (S-3) ----
    has_tests = ("test" in spec_lower) or ("## verification" in spec_lower)
    checks.append({
        "name": "test_coverage",
        "passed": has_tests,
        "emoji": "\u2705" if has_tests else "\u274c",
        "detail": (
            "Testing/verification approach described"
            if has_tests
            else "No testing or verification approach described"
        ),
    })
    if not has_tests:
        suggested_improvements.append(
            "Describe how this will be tested/verified (## Verification section or tests)"
        )

    # ---- Check 4: Tool usage ----
    tool_patterns = ['anchor("safe")', "anchor('safe')", 'anchor("test")', "anchor('test')", 'anchor("gate")', "anchor('gate')"]
    has_tool_refs = any(t in spec_text for t in tool_patterns)
    checks.append({
        "name": "tool_usage",
        "passed": has_tool_refs,
        "emoji": "\u2705" if has_tool_refs else "\u26a0\ufe0f",
        "detail": "Spec references anchor() tools" if has_tool_refs else "No anchor() tool references found",
    })
    if not has_tool_refs:
        suggested_improvements.append("Add anchor() tool references in phases (safe, touched, test, gate)")

    # ---- Check 5: Permission declaration ----
    has_permissions = len(permissions) > 0 or "permissions_needed" in spec_text
    checks.append({
        "name": "permission_declaration",
        "passed": has_permissions,
        "emoji": "\u2705" if has_permissions else "\u26a0\ufe0f",
        "detail": f"Permissions declared: {permissions}" if permissions else "permissions_needed present (empty)",
    })
    if not has_permissions:
        suggested_improvements.append("Add permissions_needed to YAML frontmatter")

    # ---- Check 6: Scope sanity ----
    scope_ok = len(files_touched) <= 10
    checks.append({
        "name": "scope_sanity",
        "passed": scope_ok,
        "emoji": "\u2705" if scope_ok else "\u26a0\ufe0f",
        "detail": f"{len(files_touched)} files in scope" if scope_ok else f"{len(files_touched)} files \u2014 consider splitting",
    })
    if not scope_ok:
        suggested_improvements.append(f"Reduce scope: {len(files_touched)} files is large")

    # ---- Check 7: Risk assessment ----
    needs_risks = mode in ("implementation", "migration", "debugging")
    has_risks = "## risks" in spec_lower or "## risk" in spec_lower
    risk_ok = has_risks or not needs_risks
    checks.append({
        "name": "risk_assessment",
        "passed": risk_ok,
        "emoji": "\u2705" if risk_ok else "\u274c",
        "detail": "Risks documented" if has_risks else ("Not required for this mode" if not needs_risks else "Missing ## Risks section"),
    })
    if not risk_ok:
        suggested_improvements.append("Add ## Risks section with risks and mitigations")

    # ---- Check 8: Standards cross-reference ----
    from odibi_anchor.planning._task_builders import required_skills_for_task
    from odibi_anchor.planning._task_profile import normalize_task_profile
    from odibi_anchor._dispatcher._boot import _resolve_skills_dir
    profile = normalize_task_profile(
        legacy_mode=mode,
        task_text=spec_text,
        traits=("specification",) if mode == "spec_creation" else None,
    )
    required_skills = required_skills_for_task(
        profile,
        specification_disposition="required" if mode == "spec_creation" else "not_required",
        explicit_formal_spec=mode == "spec_creation",
    )
    skills_referenced: list[str] = []
    skills_missing: list[str] = []
    skills_dir = Path(_resolve_skills_dir())

    for skill_name in required_skills:
        skill_path = skills_dir / skill_name / "SKILL.md"
        if not skill_path.exists():
            continue  # Skill file missing — skip, don't penalize
        # Check if spec references this skill by name or directory
        if skill_name in spec_lower or skill_name.replace("-", "_") in spec_lower:
            skills_referenced.append(skill_name)
        else:
            # Deeper check: read skill for key requirements and check spec
            try:
                skill_text = skill_path.read_text(encoding="utf-8").lower()
                # Extract requirement keywords from skill headings and bold items
                skill_keywords = []
                for line in skill_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("### "):
                        skill_keywords.append(stripped[4:].strip())
                    elif stripped.startswith("**") and stripped.endswith("**"):
                        skill_keywords.append(stripped.strip("* ").lower())
                # Check if spec references any key concepts from the skill
                matched = any(kw in spec_lower for kw in skill_keywords if len(kw) > 5)
                if matched:
                    skills_referenced.append(skill_name)
                else:
                    skills_missing.append(skill_name)
            except OSError:
                continue

    has_standards_ref = len(skills_missing) == 0 or not required_skills
    checks.append({
        "name": "standards_cross_ref",
        "passed": has_standards_ref,
        "emoji": "\u2705" if has_standards_ref else "\u274c",
        "detail": (
            f"Spec references required skills: {', '.join(skills_referenced)}"
            if has_standards_ref
            else f"Spec missing references to required skills: {', '.join(skills_missing)}"
        ),
    })
    if skills_missing:
        suggested_improvements.append(
            f"Reference Odibi Anchor standards: {', '.join(skills_missing)}"
        )

    # ---- Overall ----
    passed_count = sum(1 for c in checks if c["passed"])
    total_count = len(checks)
    all_passed = passed_count == total_count
    has_errors = any(c["emoji"] == "\u274c" for c in checks)
    rating = "excellent" if all_passed else "good" if passed_count >= 5 else "needs-work" if passed_count >= 3 else "poor"

    # ---- Persist review section ----
    review_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    review_block = f"\n\n## Review\n\n**Date:** {review_ts}  \n**Rating:** {rating} ({passed_count}/{total_count})\n\n"
    for c in checks:
        review_block += f"- {c['emoji']} **{c['name']}**: {c['detail']}\n"
    if suggested_improvements:
        review_block += "\n**Improvements:**\n\n"
        for imp in suggested_improvements:
            review_block += f"- {imp}\n"

    if "## Review" in spec_text:
        import re as _re
        spec_text = _re.sub(
            r"\n## Review.*?(?=\n## |\Z)", review_block.rstrip(), spec_text, flags=_re.DOTALL
        )
    else:
        spec_text += review_block
    spec_path.write_text(spec_text, encoding="utf-8")

    lines_out = [
        f"## Spec Review: {spec_name}",
        "",
        f"**Rating:** {rating} ({passed_count}/{total_count})",
        "",
    ]
    for c in checks:
        lines_out.append(f"  {c['emoji']} **{c['name']}** \u2014 {c['detail']}")
    if suggested_improvements:
        lines_out.append("")
        lines_out.append("### Improvements")
        for imp in suggested_improvements:
            lines_out.append(f"  - {imp}")

    # Always return dict so post-dispatch can detect "rating" and set
    # session_state.spec_review_rating regardless of output_format.
    result_dict = {
        "spec_name": spec_name,
        "write_performed": True,
        "artifact_path": str(spec_path),
        "rating": rating,
        "passed": passed_count,
        "total": total_count,
        "all_passed": all_passed,
        "has_errors": has_errors,
        "checks": checks,
        "suggested_improvements": suggested_improvements,
        "review_persisted": True,
        "summary": "\n".join(lines_out),
    }
    return result_dict
