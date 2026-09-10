"""odibi_anchor.debugging.failure_pattern_context — Known failure lookup.

Matches error text against a project-local failure_patterns.yaml to short-circuit
agent debugging loops. Returns the known root cause, fix, and anti-patterns to
avoid. Designed to compose with error_trace_context.

Usage:
    from odibi_anchor.debugging import failure_pattern_context

    ctx = failure_pattern_context(
        error_text="fixture 'root' not found",
        patterns_path="/path/to/failure_patterns.yaml",
    )
    print(ctx["matched_patterns"][0]["fix"])
    # "rename source file OR alias import with underscore prefix"

Dependencies: stdlib only (re, pathlib, yaml via safe subset).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format, build_base_context
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


def failure_pattern_context(
    error_text: str,
    *,
    root: str | Path | None = None,
    patterns_path: str | Path | None = None,
    patterns: list[dict[str, Any]] | None = None,
    subject: str | None = None,
    max_matches: int = 3,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Match error text against known failure patterns.

    Searches a list of failure patterns (from a YAML file or passed directly)
    for matches against the error text. Returns matched patterns with root
    cause, fix, and never_try anti-patterns.

    Args:
        error_text: Raw error text, traceback, or error message to match against.
        root: Project root directory. When provided and patterns_path is None,
            auto-discovers *pattern*.yaml files in root and root's immediate
            subdirectories.
        patterns_path: Path to a failure_patterns.yaml file. The file uses
            a simple YAML-subset format (parsed with stdlib, no PyYAML needed).
            If root, patterns_path, and patterns are all None, returns an empty match.
        patterns: List of pattern dicts passed directly (alternative to file).
            Each dict has keys: pattern (str/regex), context (str),
            root_cause (str), fix (str), never_try (list[str]).
        subject: Human label for the error. Defaults to first 60 chars of error.
        max_matches: Maximum number of matched patterns to return.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).

    Example:
        >>> ctx = failure_pattern_context(
        ...     "fixture 'root' not found",
        ...     patterns=[{
        ...         "pattern": "fixture .* not found",
        ...         "context": "module with test_ prefix imported into test file",
        ...         "root_cause": "pytest collects imported modules named test_*",
        ...         "fix": "rename source file OR alias import with underscore prefix",
        ...         "never_try": ["collect_ignore_glob", "conftest hooks for imported modules"],
        ...     }],
        ... )
        >>> ctx["matched_patterns"][0]["root_cause"]
        'pytest collects imported modules named test_*'
    """
    validate_output_format(output_format)

    subject = subject or (error_text[:60].strip().replace("\n", " ") + "..." if len(error_text) > 60 else error_text.strip())
    error_lower = error_text.lower()

    # Load patterns
    all_patterns = list(patterns or [])
    if patterns_path:
        file_patterns = _load_patterns_file(Path(patterns_path))
        all_patterns.extend(file_patterns)
    elif root and not all_patterns:
        # Auto-discover pattern files when no explicit path given
        discovered = _auto_discover_pattern_files(Path(root).resolve())
        for pf in discovered:
            all_patterns.extend(_load_patterns_file(pf))

    # Match
    matched = _match_patterns(error_text, error_lower, all_patterns, max_matches)

    # Build output
    metrics = {
        "patterns_searched": len(all_patterns),
        "patterns_matched": len(matched),
        "has_match": len(matched) > 0,
        "auto_discovered": root is not None and patterns_path is None,
    }

    findings = []
    risks = []
    never_try_all: list[str] = []

    if matched:
        findings.append(f"{len(matched)} known failure pattern(s) matched.")
        for m in matched:
            findings.append(f"Match: {m['pattern']} \u2192 {m['root_cause']}")
            never_try_all.extend(m.get("never_try", []))
        if never_try_all:
            risks.append(
                f"Known anti-patterns \u2014 do NOT try: {'; '.join(never_try_all)}"
            )
    else:
        findings.append("No known failure patterns matched \u2014 this may be a new error type.")

    suggested_actions = []
    if matched:
        for m in matched:
            suggested_actions.append(f"MUST: Apply fix \u2014 {m['fix']}")
        if never_try_all:
            suggested_actions.append(
                f"MUST: Avoid these approaches: {', '.join(never_try_all)}"
            )
    else:
        suggested_actions.append(
            "MUST: Run anchor('trace', 'error text') for structured traceback analysis."
        )
        suggested_actions.append(
            "COULD: At learning closure, capture a supported reusable failure pattern; "
            "a solved error does not itself require a save."
        )

    # anchor() workflow hints
    suggested_actions.append(
        "MUST: Run anchor('preflight', changed_files=[...]) after applying any fix."
    )

    if matched:
        summary = (
            f"Matched {len(matched)} known pattern(s). "
            f"Root cause: {matched[0]['root_cause']}. "
            f"Fix: {matched[0]['fix']}."
        )
    else:
        summary = f"No known patterns matched for: {subject}"

    ctx = build_base_context(
        kind="failure_pattern_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={
            "never_try": never_try_all,
        },
        suggested_next_actions=suggested_actions,
        matched_patterns=matched,
    )

    if output_format == "markdown":
        return render_failure_pattern_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Pattern Loading (stdlib YAML subset — no PyYAML dependency)
# ---------------------------------------------------------------------------



def _auto_discover_pattern_files(root: Path) -> list[Path]:
    """Auto-discover *pattern*.yaml files in root and immediate subdirectories.

    Searches:
      - root/*pattern*.yaml
      - root/*pattern*.yml
      - root/.patterns/*.yaml (if directory exists)

    Returns list of discovered file paths, deduplicated.
    """
    discovered: list[Path] = []
    seen: set[Path] = set()

    # Direct glob in root
    for ext in ("yaml", "yml"):
        for p in root.glob(f"*pattern*.{ext}"):
            if p.is_file() and p not in seen:
                discovered.append(p)
                seen.add(p)

    # Check .patterns/ subdirectory
    patterns_dir = root / ".patterns"
    if patterns_dir.is_dir():
        for ext in ("yaml", "yml"):
            for p in patterns_dir.glob(f"*.{ext}"):
                if p.is_file() and p not in seen:
                    discovered.append(p)
                    seen.add(p)

    return sorted(discovered)


def _load_patterns_file(path: Path) -> list[dict[str, Any]]:
    """Load patterns from a YAML file using stdlib-only parsing.

    Supports a simple YAML subset: list of dicts with string values and
    string lists. This avoids requiring PyYAML as a dependency.

    Falls back gracefully if the file doesn't exist or can't be parsed.
    """
    if not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    # Try PyYAML first if available, fall back to simple parser
    try:
        import yaml  # noqa: F811
        data = yaml.safe_load(content)
        if isinstance(data, list):
            return [_normalize_pattern(p) for p in data if isinstance(p, dict)]
        return []
    except ImportError:
        pass

    # Stdlib fallback: simple line-based parser for our known format
    return _parse_simple_yaml(content)


def _parse_simple_yaml(content: str) -> list[dict[str, Any]]:
    """Parse the simple YAML subset used by failure_patterns.yaml.

    Handles:
      - pattern: "text"
      - key: value
      - never_try: ["item1", "item2"]  (inline list)
      - never_try:
        - "item1"
        - "item2"
    """
    patterns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_list_key: str | None = None

    for raw_line in content.splitlines():
        stripped = raw_line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            if current_list_key and not stripped:
                current_list_key = None
            continue

        # New pattern entry (starts with "- pattern:")
        if stripped.startswith("- pattern:"):
            if current:
                patterns.append(_normalize_pattern(current))
            current = {}
            current_list_key = None
            value = stripped[len("- pattern:"):].strip().strip('"').strip("'")
            current["pattern"] = value
            continue

        # List item under a key (e.g., "  - item")
        if current is not None and stripped.startswith("- ") and current_list_key:
            value = stripped[2:].strip().strip('"').strip("'")
            if current_list_key not in current:
                current[current_list_key] = []
            current[current_list_key].append(value)
            continue

        # Key: value pair (indented under current pattern)
        if current is not None and ":" in stripped:
            # Handle "  key: value" lines
            line_content = stripped.lstrip("- ").strip()
            colon_idx = line_content.index(":")
            key = line_content[:colon_idx].strip()
            value = line_content[colon_idx + 1:].strip()

            # Check for inline list: ["a", "b"]
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                items = [
                    item.strip().strip('"').strip("'")
                    for item in inner.split(",")
                    if item.strip()
                ]
                current[key] = items
                current_list_key = None
            elif value:
                current[key] = value.strip('"').strip("'")
                current_list_key = None
            else:
                # Empty value — next lines might be list items
                current_list_key = key
                current[key] = []

    if current:
        patterns.append(_normalize_pattern(current))

    return patterns


def _normalize_pattern(raw: dict) -> dict[str, Any]:
    """Ensure pattern dict has all expected keys."""
    return {
        "pattern": str(raw.get("pattern", "")),
        "context": str(raw.get("context", "")),
        "root_cause": str(raw.get("root_cause", "")),
        "fix": str(raw.get("fix", "")),
        "never_try": list(raw.get("never_try", [])),
    }


# ---------------------------------------------------------------------------
# Pattern Matching
# ---------------------------------------------------------------------------


def _match_patterns(
    error_text: str,
    error_lower: str,
    patterns: list[dict[str, Any]],
    max_matches: int,
) -> list[dict[str, Any]]:
    """Match error text against patterns using regex and substring matching."""
    scored: list[tuple[float, dict[str, Any]]] = []

    for pat in patterns:
        pattern_str = pat.get("pattern", "")
        if not pattern_str:
            continue

        score = 0.0

        # Try regex match first
        try:
            if re.search(pattern_str, error_text, re.IGNORECASE):
                score = 1.0
        except re.error:
            pass

        # Fall back to substring match
        if score == 0 and pattern_str.lower() in error_lower:
            score = 0.8

        # Boost if context also matches
        context = pat.get("context", "")
        if context and score > 0:
            if context.lower() in error_lower:
                score += 0.2

        if score > 0:
            scored.append((score, pat))

    # Sort by score descending, take top N
    scored.sort(key=lambda x: x[0], reverse=True)
    return [pat for _, pat in scored[:max_matches]]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_failure_pattern_report(ctx: dict[str, Any]) -> str:
    """Render failure_pattern_context output as markdown."""
    lines = render_header_lines(ctx, "Failure Pattern Lookup")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    matched = ctx.get("matched_patterns", [])
    if matched:
        lines.extend(["", "## Matched Patterns", ""])
        for i, m in enumerate(matched, 1):
            lines.append(f"### Match {i}: `{m['pattern']}`")
            lines.append("")
            if m.get("context"):
                lines.append(f"**Context:** {m['context']}")
            lines.append(f"**Root cause:** {m['root_cause']}")
            lines.append(f"**Fix:** {m['fix']}")
            if m.get("never_try"):
                lines.append("")
                lines.append("**\u26d4 Never try:**")
                for nt in m["never_try"]:
                    lines.append(f"- {nt}")
            lines.append("")
    else:
        lines.extend(["", "No known patterns matched.", ""])

    lines.extend(render_bullet_section(ctx["findings"], "## Findings"))
    lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
    lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Next Actions"))

    return "\n".join(lines)
