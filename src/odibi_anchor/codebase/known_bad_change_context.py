"""odibi_anchor.codebase.known_bad_change_context — Pre-edit guardrail.

Given a proposed edit (as a diff string, or action + target description),
checks memory and failure patterns for similar past failures. Returns
a warn/block signal BEFORE the edit is applied.

Usage:
    from odibi_anchor.codebase.known_bad_change_context import known_bad_change_context

    ctx = known_bad_change_context(
        root="/path/to/project",
        proposed_diff="--- a/foo.py\n+++ b/foo.py\n...",
        changed_files=["src/odibi_anchor/codebase/safe_change_context.py"],
        action="rename_parameter",
        task_type="refactor",
    )
    if ctx["metrics"]["status"] == "block":
        print("BLOCKED:", ctx["risks"])

Dependencies: stdlib only (json, re, pathlib). Uses memory_context and
failure_pattern_context internally.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from odibi_anchor._utils.contract import build_base_context, validate_output_format
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
)

# ---------------------------------------------------------------------------
# Static Anti-Pattern Rules (proactive — no memory required)
# ---------------------------------------------------------------------------

_STATIC_ANTI_PATTERNS: list[dict[str, str]] = [
    {
        "id": "collect_large",
        "pattern": r"\.collect\(\)",
        "message": "DataFrame.collect() pulls all data to driver — use Spark operations or limit first",
        "severity": "warn",
        "category": "performance",
        "applies_to": "spark",
    },
    {
        "id": "to_pandas_unbounded",
        "pattern": r"\.toPandas\(\)",
        "message": "toPandas() materializes entire DataFrame on driver — use .limit() or Spark-native ops",
        "severity": "warn",
        "category": "performance",
        "applies_to": "spark",
    },
    {
        "id": "cast_without_try",
        "pattern": r"(?<!TRY_)CAST\(",
        "message": "CAST() fails on bad data — use TRY_CAST() with NULLIF(TRIM(col), '') for safety",
        "severity": "warn",
        "category": "correctness",
        "applies_to": "spark_sql",
    },
    {
        "id": "import_star",
        "pattern": r"^\s*from\s+\S+\s+import\s+\*",
        "message": "Wildcard imports pollute namespace and hide dependencies — import explicitly",
        "severity": "warn",
        "category": "maintainability",
    },
    {
        "id": "dbfs_mount_path",
        "pattern": r"/(dbfs|mnt)/",
        "message": "/dbfs/ and /mnt/ paths are deprecated — use Unity Catalog volumes or ABFSS",
        "severity": "warn",
        "category": "migration",
        "applies_to": "databricks",
    },
    {
        "id": "time_sleep_production",
        "pattern": r"time\.sleep\(",
        "message": "time.sleep() in production code blocks the thread — use retry/backoff or event-driven",
        "severity": "warn",
        "category": "reliability",
    },
    {
        "id": "hardcoded_credentials",
        "pattern": r"(password|secret|token|api_key)\s*=\s*[\x22\x27][^\x22\x27]",
        "message": "Possible hardcoded credential — use the environment or an approved secret provider",
        "severity": "warn",
        "category": "security",
        "control_class": "secret",
    },
    {
        "id": "display_in_module",
        "pattern": r"^\s*display\(",
        "message": "display() in module code fails outside notebooks — guard with try/except or use logging",
        "severity": "warn",
        "category": "portability",
        "applies_to": "databricks",
    },
    # --- Spark / PySpark ---
    {
        "id": "rdd_usage",
        "pattern": r"\.rdd[.\[]",
        "message": "RDD API bypasses Catalyst optimizer and breaks Spark Connect — use DataFrame API",
        "severity": "warn",
        "category": "performance",
        "applies_to": "spark",
    },
    {
        "id": "spark_context_direct",
        "pattern": r"(sc\.|sparkContext|SparkContext)",
        "message": "SparkContext is incompatible with Spark Connect and USER_ISOLATION — use SparkSession",
        "severity": "warn",
        "category": "compatibility",
        "applies_to": "spark",
    },
    {
        "id": "udf_without_pandas",
        "pattern": r"@udf|spark\.udf\.register",
        "message": "Row-at-a-time UDFs are slow — prefer pandas_udf or native Spark functions",
        "severity": "warn",
        "category": "performance",
        "applies_to": "spark",
    },
    {
        "id": "cache_persist_unmanaged",
        "pattern": r"\.(cache|persist)\(\)",
        "message": ".cache()/.persist() without unpersist() leaks memory — prefer checkpointing or let Spark manage",
        "severity": "warn",
        "category": "reliability",
        "applies_to": "spark",
    },
    {
        "id": "count_for_emptiness",
        "pattern": r"\.count\(\)\s*[=><!]+\s*0|if.*\.count\(\)",
        "message": "count() scans entire dataset — use .head(1), .isEmpty, or .limit(1).count() for emptiness checks",
        "severity": "warn",
        "category": "performance",
        "applies_to": "spark",
    },
    {
        "id": "crossjoin_implicit",
        "pattern": r"\.crossJoin\(|join.*how=[\x22\x27]cross",
        "message": "Cross joins produce N*M rows — verify this is intentional and not a missing join condition",
        "severity": "warn",
        "category": "correctness",
        "applies_to": "spark",
    },
    # --- Delta / Data Quality ---
    {
        "id": "overwrite_no_condition",
        "pattern": r"mode\([\x22\x27]overwrite[\x22\x27]\)",
        "message": "Unconditional overwrite destroys history — prefer merge/upsert or partition overwrite",
        "severity": "warn",
        "category": "data_safety",
        "applies_to": "delta",
    },
    {
        "id": "schema_star_select",
        "pattern": r"select\([\x22\x27]\*[\x22\x27]\)|SELECT\s+\*\s+FROM",
        "message": "SELECT * is fragile — explicitly name columns to prevent schema drift breakage",
        "severity": "warn",
        "category": "maintainability",
        "applies_to": "sql",
    },
    {
        "id": "no_null_handling",
        "pattern": r"\.cast\([\x22\x27](int|long|double|float)",
        "message": "Casting without null handling — nulls will propagate silently. Use coalesce() or filter first",
        "severity": "warn",
        "category": "correctness",
        "applies_to": "spark",
    },
    # --- Python / General ---
    {
        "id": "bare_except",
        "pattern": r"except\s*:",
        "message": "Bare except catches SystemExit/KeyboardInterrupt — use except Exception:",
        "severity": "warn",
        "category": "correctness",
    },
    {
        "id": "mutable_default_arg",
        "pattern": r"def\s+\w+\([^)]*=\s*\[|def\s+\w+\([^)]*=\s*\{",
        "message": "Mutable default argument (list/dict) is shared across calls — use None + conditional",
        "severity": "warn",
        "category": "correctness",
    },
    {
        "id": "print_instead_of_log",
        "pattern": r"^\s*print\(",
        "message": "print() in production code — use logging module or framework logger for observability",
        "severity": "warn",
        "category": "observability",
    },
    {
        "id": "todo_fixme_hack",
        "pattern": r"#\s*(TODO|FIXME|HACK|XXX|TEMP)",
        "message": "Unresolved TODO/FIXME — address before merging or document as tech debt",
        "severity": "warn",
        "category": "maintainability",
    },
    {
        "id": "string_concat_sql",
        "pattern": r"f[\x22\x27].*SELECT.*\{|[\x22\x27].*SELECT.*\x22\s*\+",
        "message": "String-concatenated SQL is vulnerable to injection — use parameterized queries or spark.sql with params",
        "severity": "warn",
        "category": "security",
    },
    {
        "id": "global_state_mutation",
        "pattern": r"^\s*global\s+\w|^\s*\w+\.append\(|^\s*\w+\.update\(",
        "message": "Global state mutation makes code non-deterministic — prefer functional patterns or class encapsulation",
        "severity": "warn",
        "category": "maintainability",
    },
]

_NON_SUPPRESSIBLE_CONTROL_CLASSES = frozenset({"secret", "effect", "evidence"})
_TASK_DOMAIN_PROFILES = {
    "spark": {"spark", "sql", "delta"},
    "pyspark": {"spark", "sql", "delta"},
    "databricks": {"databricks", "spark", "sql", "delta"},
    "dbfs": {"databricks"},
    "unity-catalog": {"databricks", "sql"},
    "unity_catalog": {"databricks", "sql"},
    "sql": {"sql"},
    "delta": {"delta", "spark", "sql"},
    "delta-lake": {"delta", "spark", "sql"},
}


def _applicable_profiles(
    *,
    root: Path | None,
    changed_files: list[str],
    code_text: str,
    task_type: str | None,
) -> set[str]:
    """Resolve technology profiles from concrete paths, dependencies, imports, or task domain."""
    profiles = {"universal"}
    normalized_task_type = task_type.strip().lower() if isinstance(task_type, str) else ""
    profiles.update(_TASK_DOMAIN_PROFILES.get(normalized_task_type, ()))

    path_tokens: set[str] = set()
    for file_name in changed_files:
        path = Path(file_name)
        if path.suffix.lower() in {".py", ".pyi"}:
            profiles.add("python")
        if path.suffix.lower() == ".sql":
            profiles.add("sql")
        path_tokens.update(
            token for part in path.parts
            for token in re.split(r"[^a-z0-9]+", Path(part).stem.lower()) if token
        )
    if path_tokens & {"spark", "pyspark"}:
        profiles.update({"spark", "sql", "delta"})
    if path_tokens & {"databricks", "dbfs", "unity"}:
        profiles.update({"databricks", "sql"})
    if "delta" in path_tokens:
        profiles.update({"delta", "spark", "sql"})

    import_text = "\n".join(
        line for line in code_text.splitlines()
        if re.match(r"^\s*(?:from|import)\s+", line.lstrip("+- "))
    )
    if re.search(r"\b(?:pyspark|delta)(?:\.|\s|$)", import_text, re.IGNORECASE):
        profiles.update({"spark", "sql", "delta"})
    if re.search(r"\bdatabricks(?:\.|\s|$)", import_text, re.IGNORECASE):
        profiles.add("databricks")

    if root is not None:
        dependency_text = ""
        for relative in ("pyproject.toml", "requirements.txt", "requirements-dev.txt"):
            path = root / relative
            try:
                dependency_text += "\n" + "\n".join(
                    line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
                )
            except OSError:
                continue
        if re.search(r"\b(?:pyspark|delta-spark)\b", dependency_text, re.IGNORECASE):
            profiles.update({"spark", "sql", "delta"})
        if re.search(r"\bdatabricks(?:-[a-z0-9-]+)?\b", dependency_text, re.IGNORECASE):
            profiles.add("databricks")
    return profiles


def _rule_is_applicable(rule: dict[str, str], profiles: set[str]) -> bool:
    applies_to = rule.get("applies_to", "universal")
    if applies_to == "spark_sql":
        return "spark" in profiles and "sql" in profiles
    return applies_to in profiles


def _load_suppress_config(root: Path | None = None) -> dict[str, Any]:
    """Load anti-pattern suppress configuration from project root.

    Looks for `.anchor_config.json` at the project root. Expected structure:
        {
            "anti_patterns": {
                "suppress_categories": ["maintainability", "observability"],
                "suppress_ids": ["todo_fixme_hack", "print_instead_of_log"],
                "file_overrides": {
                    "tests/**": {"suppress_categories": ["performance", "portability"]},
                    "scripts/**": {"suppress_ids": ["print_instead_of_log"]}
                }
            }
        }

    Returns empty dict if file not found or invalid.
    """
    if not root:
        return {}
    config_path = root / ".anchor_config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        return config.get("anti_patterns", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _check_static_patterns(
    code_text: str,
    *,
    file_path: str | None = None,
    patterns: list[dict[str, str]] | None = None,
    suppress_categories: list[str] | None = None,
    suppress_ids: list[str] | None = None,
    root: Path | None = None,
    changed_files: list[str] | None = None,
    task_type: str | None = None,
) -> list[dict[str, Any]]:
    """Check code text against static anti-pattern rules.

    Args:
        code_text: The code to scan (raw body, diff lines, or full file content).
        file_path: Optional file path for context-aware filtering (e.g., skip
            time.sleep warning in test files).
        patterns: Override pattern list (defaults to _STATIC_ANTI_PATTERNS).
        suppress_categories: Categories to skip entirely (e.g., ["performance"]).
        suppress_ids: Specific pattern IDs to skip (e.g., ["todo_fixme_hack"]).
        root: Project root — loads .anchor_config.json and repository dependency signals.
        changed_files: Concrete changed paths used for technology applicability.
        task_type: Canonical task domain; arbitrary prose is not interpreted.

    Suppression priority (highest to lowest):
        1. Secret, effect, and evidence controls are never suppressible
        2. Explicit suppress_categories/suppress_ids kwargs
        3. File-level overrides from .anchor_config.json (matched by glob)
        4. Project-level suppression from .anchor_config.json

    Returns:
        List of matched pattern dicts with keys: id, message, severity, category,
        matched_line (first matching line text).
    """
    if not code_text:
        return []

    # Load project config
    project_config = _load_suppress_config(root)

    # Build effective suppress sets
    # None = unset (use project config), [] = explicitly empty (suppress nothing)
    effective_categories: set[str] = set()
    effective_ids: set[str] = set()

    if suppress_categories is not None:
        # Explicit kwarg takes precedence — don't layer config
        effective_categories = set(suppress_categories)
    elif project_config.get("suppress_categories"):
        effective_categories = set(project_config["suppress_categories"])

    if suppress_ids is not None:
        # Explicit kwarg takes precedence — don't layer config
        effective_ids = set(suppress_ids)
    elif project_config.get("suppress_ids"):
        effective_ids = set(project_config["suppress_ids"])

    # File-level overrides from config
    file_overrides = project_config.get("file_overrides", {})
    if file_path and file_overrides:
        for glob_pattern, override in file_overrides.items():
            if fnmatch(file_path, glob_pattern) or fnmatch(file_path, f"**/{glob_pattern}"):
                effective_categories.update(override.get("suppress_categories", []))
                effective_ids.update(override.get("suppress_ids", []))

    rules = patterns or _STATIC_ANTI_PATTERNS
    matches: list[dict[str, Any]] = []
    is_test_file = file_path and ("test_" in file_path or "_test.py" in file_path)
    concrete_files = list(changed_files or ([file_path] if file_path else []))
    profiles = _applicable_profiles(
        root=root,
        changed_files=concrete_files,
        code_text=code_text,
        task_type=task_type,
    )

    for rule in rules:
        if not _rule_is_applicable(rule, profiles):
            continue
        protected = rule.get("control_class") in _NON_SUPPRESSIBLE_CONTROL_CLASSES
        if not protected and rule["category"] in effective_categories:
            continue
        if not protected and rule["id"] in effective_ids:
            continue
        # Skip time.sleep warning in test files (common in integration tests)
        if rule["id"] == "time_sleep_production" and is_test_file:
            continue

        pattern = re.compile(rule["pattern"], re.MULTILINE)
        for line in code_text.splitlines():
            # Strip unified diff prefix (+/-/space) for pattern matching
            # but keep original line for reporting
            clean_line = line
            if line and line[0] in ("+", "-", " "):
                clean_line = line[1:]
            if pattern.search(clean_line):
                matches.append({
                    "id": rule["id"],
                    "message": rule["message"],
                    "severity": rule["severity"],
                    "category": rule["category"],
                    "matched_line": line.strip()[:120],
                })
                break  # One match per rule is enough

    return matches


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def known_bad_change_context(
    root: str | Path,
    *,
    proposed_diff: str | None = None,
    changed_files: list[str] | None = None,
    action: str | None = None,
    task_type: str | None = None,
    error_text: str | None = None,
    memory_path: str | None = None,
    patterns_path: str | None = None,
    subject: str | None = None,
    output_format: str = "dict",
    db_path: str | None = None,
    project: str | None = None,
) -> dict[str, Any] | str:
    """Pre-edit guardrail: check proposed change against known failures.

    Queries project memory (gotchas and failure_patterns) and optionally
    failure_pattern_context for matches against the proposed change. Returns
    a status signal (ok/warn/block) to decide whether to proceed.

    Args:
        root: Project root directory (memory stored in .agent_memory.db).
        proposed_diff: Unified diff string of the proposed change.
        changed_files: List of file paths being modified.
        action: Action being performed (e.g., "rename_parameter", "add_import").
        task_type: Task type context (e.g., "refactor", "debug").
        error_text: Optional error text that triggered this change.
        memory_path: Override path to memory JSONL file.
        patterns_path: Path to failure_patterns.yaml for extended matching.
        subject: Human label. Defaults to action or directory name.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string) with status in metrics.
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    subject = subject or action or root.name

    # Extract tokens from the diff for matching
    diff_tokens = _extract_diff_tokens(proposed_diff or "")

    # --- Query memory for gotchas and failure_patterns ---
    from odibi_anchor.codebase.memory_context import memory_context as _query_memory

    # Query gotchas
    gotcha_ctx = _query_memory(
        root,
        files_involved=changed_files,
        tags=[t for t in [action, task_type] if t] or None,
        error_text=error_text,
        entry_type="gotcha",
        limit=20,
        surfaced=False,
        db_path=db_path,
        project=project,
    )
    gotcha_entries = gotcha_ctx.get("entries", []) if isinstance(gotcha_ctx, dict) else []

    # Query failure_patterns
    fp_ctx = _query_memory(
        root,
        files_involved=changed_files,
        tags=[t for t in [action, task_type] if t] or None,
        error_text=error_text,
        entry_type="failure_pattern",
        limit=20,
        surfaced=False,
        db_path=db_path,
        project=project,
    )
    fp_entries = fp_ctx.get("entries", []) if isinstance(fp_ctx, dict) else []

    all_memory_entries = gotcha_entries + fp_entries

    # --- Optionally query failure_pattern_context ---
    matched_patterns: list[dict[str, Any]] = []
    patterns_checked = 0
    if patterns_path:
        try:
            from odibi_anchor.debugging.failure_pattern_context import failure_pattern_context
            # Build error_text from diff tokens if none provided
            search_text = error_text or " ".join(diff_tokens[:50])
            if search_text.strip():
                pat_ctx = failure_pattern_context(
                    search_text,
                    patterns_path=patterns_path,
                )
                if isinstance(pat_ctx, dict):
                    matched_patterns = pat_ctx.get("matched_patterns", [])
                    patterns_checked = pat_ctx.get("metrics", {}).get("total_patterns", 0)
        except Exception as exc:
            matched_patterns = [{"pattern": "⚠️ DEGRADED", "message": f"Pattern lookup failed: {type(exc).__name__}: {exc}"}]

    # --- Static anti-pattern scan on proposed diff ---
    static_matches: list[dict[str, Any]] = []
    if proposed_diff:
        # Scan the actual diff content
        file_hint = changed_files[0] if changed_files else None
        static_matches = _check_static_patterns(
            proposed_diff,
            file_path=file_hint,
            root=root,
            changed_files=changed_files,
            task_type=task_type,
        )
    elif changed_files:
        # No diff provided — scan the actual files for anti-patterns
        # (lightweight: only check files being modified, not the whole project)
        for cf in changed_files[:3]:  # cap at 3 files to stay fast
            abs_cf = Path(cf) if Path(cf).is_absolute() else root / cf
            if abs_cf.is_file():
                try:
                    file_source = abs_cf.read_text(encoding="utf-8")
                    file_matches = _check_static_patterns(
                        file_source,
                        file_path=str(cf),
                        root=root,
                        changed_files=changed_files,
                        task_type=task_type,
                    )
                    static_matches.extend(file_matches)
                except Exception as exc:
                    static_matches.append({
                        "pattern_id": "scan_error",
                        "message": f"⚠️ File scan degraded for {cf}: {type(exc).__name__}: {exc}",
                        "severity": "warning",
                        "line": 0,
                    })

    # --- Score each memory entry against the proposed change ---
    scored_memories: list[dict[str, Any]] = []
    for entry in all_memory_entries:
        score = _score_against_change(
            entry,
            changed_files=changed_files or [],
            action=action or "",
            diff_tokens=diff_tokens,
        )
        if score > 0:
            scored_memories.append({**entry, "_guardrail_score": score})

    # Filter out low-score matches (recency alone shouldn't trigger)
    scored_memories = [m for m in scored_memories if m.get("_guardrail_score", 0) >= 1.0]

    # Sort by score descending
    scored_memories.sort(key=lambda e: e.get("_guardrail_score", 0), reverse=True)

    # --- Classify status ---
    blocking = [
        m for m in scored_memories
        if m.get("confidence", 0.5) >= 0.8
        and m.get("confirmation_count", 0) >= 2
    ]
    warnings = [m for m in scored_memories if m not in blocking]

    if blocking:
        status = "block"
    elif warnings or static_matches:
        status = "warn"
    else:
        status = "ok"

    # --- Build output ---
    metrics = {
        "status": status,
        "memories_checked": len(all_memory_entries),
        "patterns_checked": patterns_checked,
        "memories_matched": len(scored_memories),
        "patterns_matched": len(matched_patterns),
        "static_patterns_matched": len(static_matches),
        "max_similarity_score": (
            max((m.get("_guardrail_score", 0) for m in scored_memories), default=0.0)
        ),
        "blocking_count": len(blocking),
        "warning_count": len(warnings) + len(static_matches),
    }

    findings = []
    if status == "block":
        findings.append(
            f"BLOCKED: {len(blocking)} high-confidence known-bad pattern(s) match this change."
        )
        for m in blocking[:3]:
            findings.append(f"  - [{m.get('id')}] {m.get('content', '')[:120]}")
    elif status == "warn":
        findings.append(
            f"WARNING: {len(warnings)} memory entries resemble past failures for this change."
        )
        for m in warnings[:3]:
            findings.append(f"  - [{m.get('id')}] {m.get('content', '')[:120]}")
    else:
        if not static_matches:
            findings.append("No known-bad patterns match this proposed change.")

    # Static anti-pattern findings
    if static_matches:
        findings.append(
            f"STATIC SCAN: {len(static_matches)} anti-pattern(s) detected in proposed code."
        )
        for sm in static_matches[:5]:
            findings.append(f"  - [{sm['category']}] {sm['message']}")
            findings.append(f"    Matched: {sm['matched_line']}")

    risks = []
    # Static anti-pattern risks
    for sm in static_matches:
        risks.append(
            f"Anti-pattern [{sm['id']}]: {sm['message']} "
            f"(line: {sm['matched_line'][:60]})"
        )
    if blocking:
        for m in blocking:
            risks.append(
                f"Known failure [{m.get('id')}] (confidence={m.get('confidence', '?')}, "
                f"confirmed {m.get('confirmation_count', 0)}x): {m.get('content', '')[:100]}"
            )
    if matched_patterns:
        for p in matched_patterns[:2]:
            risks.append(f"Failure pattern match: {p.get('context', p.get('pattern', ''))[:100]}")

    suggested_actions = []
    if status == "block":
        suggested_actions.append("MUST: Do NOT apply this change. Review the blocking memories and choose a different approach.")
        if blocking:
            evidence = blocking[0].get("evidence", {})
            if evidence.get("fix"):
                suggested_actions.append(f"MUST: Consider known fix: {evidence['fix'][:150]}")
    elif status == "warn":
        suggested_actions.append("MUST: Review warnings before proceeding. The change may still be valid.")
        suggested_actions.append(
            "MUST: Keep relevant candidates advisory and verify them against current evidence. "
            "Retrieval, application, evaluation, and counters do not promote; authority requires "
            "a typed verifier or separate authenticated owner requests."
        )
    
    # anchor() workflow hints
    if status == "block":
        suggested_actions.append(
            "MUST: Reconcile the blocking bounded task-memory selections with current evidence."
        )
    suggested_actions.append(
        "COULD: At learning closure, capture a supported reusable failure pattern; "
        "do not save solely because this guard fired."
    )

    summary = (
        f"Guardrail {status.upper()}: {len(scored_memories)} memory matches, "
        f"{len(matched_patterns)} pattern matches"
    )

    # Clean internal scoring keys from output
    clean_memories = [{k: v for k, v in m.items() if not k.startswith("_")} for m in scored_memories]

    ctx = build_base_context(
        kind="known_bad_change_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_actions,
        matched_memories=clean_memories,
        matched_patterns=matched_patterns,
        static_anti_patterns=static_matches,
    )

    if output_format == "markdown":
        return render_known_bad_change_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_against_change(
    entry: dict[str, Any],
    *,
    changed_files: list[str],
    action: str,
    diff_tokens: list[str],
) -> float:
    """Score a memory entry's relevance to the proposed change."""
    score = 0.0

    # File overlap: entry's related_files glob against changed_files
    entry_files = entry.get("related_files", [])
    if changed_files and entry_files:
        for cf in changed_files:
            for pattern in entry_files:
                if fnmatch(cf, pattern) or fnmatch(cf, f"**/{pattern}"):
                    score += 3.0
                    break
            for pattern in entry_files:
                if "*" not in pattern and pattern in cf:
                    score += 2.0
                    break

    # Action overlap: does the memory mention the same action?
    if action:
        action_lower = action.lower()
        entry_tags = {t.lower() for t in entry.get("tags", [])}
        content_lower = entry.get("content", "").lower()
        if action_lower in entry_tags:
            score += 2.0
        elif action_lower in content_lower:
            score += 1.5
        # Check action keywords (e.g., "rename" in "rename_parameter")
        action_parts = set(action_lower.replace("_", " ").split())
        tag_words = set()
        for t in entry_tags:
            tag_words.update(t.replace("_", " ").split())
        if action_parts & tag_words:
            score += 1.0

    # Diff content overlap: tokens from diff appearing in memory content
    if diff_tokens:
        content_lower = entry.get("content", "").lower()
        content_words = set(content_lower.split())
        diff_set = {t.lower() for t in diff_tokens}
        overlap = diff_set & content_words
        if len(overlap) >= 5:
            score += 1.5
        elif len(overlap) >= 2:
            score += 0.5

    return score


def _extract_diff_tokens(diff: str) -> list[str]:
    """Extract meaningful tokens from a unified diff string.

    Extracts function names, import paths, parameter names, and identifiers
    from added/removed lines.
    """
    if not diff:
        return []

    tokens: list[str] = []
    for line in diff.splitlines():
        # Only process added/removed lines
        if not (line.startswith("+") or line.startswith("-")):
            continue
        # Skip diff headers
        if line.startswith("+++") or line.startswith("---"):
            continue

        content = line[1:].strip()
        if not content:
            continue

        # Extract identifiers (word-like tokens, filtering noise)
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", content)
        tokens.extend(words)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in tokens:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)

    return unique


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_known_bad_change_report(ctx: dict[str, Any]) -> str:
    """Render known_bad_change_context output as markdown."""
    lines = render_header_lines(ctx, "Pre-Edit Guardrail")

    # Status badge prominently
    status = ctx.get("metrics", {}).get("status", "unknown")
    status_emoji = {"ok": "OK", "warn": "WARNING", "block": "BLOCKED"}.get(status, status.upper())
    lines.append(f"\n**Status: {status_emoji}**\n")

    lines.extend(render_metrics_lines(ctx["metrics"]))

    # Matched memories
    memories = ctx.get("matched_memories", [])
    if memories:
        lines.extend(["", "## Matched Memories", ""])
        for m in memories[:5]:
            conf = m.get("confidence", "?")
            confirms = m.get("confirmation_count", 0)
            lines.append(
                f"- **{m.get('id', '?')}** [{m.get('type', '?')}] "
                f"(conf={conf}, confirmed={confirms}x): {m.get('content', '')[:120]}"
            )

    # Matched patterns
    patterns = ctx.get("matched_patterns", [])
    if patterns:
        lines.extend(["", "## Matched Failure Patterns", ""])
        for p in patterns[:3]:
            lines.append(f"- {p.get('context', p.get('pattern', ''))[:120]}")
            if p.get("fix"):
                lines.append(f"  - Fix: {p['fix'][:100]}")

    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "## Next Actions"))

    return "\n".join(lines)
