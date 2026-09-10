"""Spec file parser — makes spec files machine-readable.

Parses YAML frontmatter (preferred) or bold-text lines (legacy fallback)
from spec files in the specs/ directory.

Phase 1 of SPEC_DRIVEN_WORKFLOW_SPEC.
"""

from __future__ import annotations

import re
from pathlib import Path as _Path


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

_VALID_STATUSES = {
    "draft", "ready", "in-progress", "in_progress", "executing", "done", "abandoned",
}

_STATUS_ALIASES = {
    "in_progress": "in-progress",
}

# Emoji prefixes to strip: ✅, 🔄, 📋, ⬜, etc.
_EMOJI_RE = re.compile(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50]+\s*", re.UNICODE)


def _normalize_status(raw: str) -> str:
    """Normalize a status string to one of the valid enum values.

    Handles: '✅ Done — all 6 rules implemented (699 lines)' → 'done'
             'ready' → 'ready'
             'in-progress' → 'in-progress'
    """
    cleaned = _EMOJI_RE.sub("", raw).strip()
    # Take first word (before any dash-separated annotation)
    first_word = cleaned.split("—")[0].split("–")[0].strip().lower()
    # Remove trailing punctuation
    first_word = re.sub(r"[^a-z\-_]", "", first_word)
    if first_word in _STATUS_ALIASES:
        return _STATUS_ALIASES[first_word]
    if first_word in _VALID_STATUSES:
        return first_word
    return "draft"  # default fallback


# ---------------------------------------------------------------------------
# YAML frontmatter parser (stdlib-only, flat keys + lists)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.+?)\n---\s*\n", re.DOTALL)
_KEY_VALUE_RE = re.compile(r"^([a-z_]+):\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s+-\s+(.+)$")


def _parse_frontmatter(text: str) -> dict | None:
    """Parse YAML-like frontmatter from a spec file.

    Returns None if no frontmatter is found.
    Only handles flat key: value pairs and lists of strings/dicts.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None

    block = match.group(1)
    result = {}
    current_key = None
    current_list: list | None = None

    for line in block.split("\n"):
        # Check for key: value
        kv = _KEY_VALUE_RE.match(line)
        if kv:
            # Save any pending list
            if current_key and current_list is not None:
                result[current_key] = current_list

            key, value = kv.group(1), kv.group(2).strip()
            if value == "[]":
                # Explicit empty list
                current_key = key
                current_list = []
            elif value == "":
                # Bare key with no value — start collecting list items
                current_key = key
                current_list = []
            elif value == "null" or value == "~":
                result[key] = None
                current_key = None
                current_list = None
            else:
                # Strip quotes and comments
                value = re.sub(r"\s*#.*$", "", value)
                value = value.strip("\"\'")
                result[key] = value
                current_key = None
                current_list = None
            continue

        # Check for list item
        li = _LIST_ITEM_RE.match(line)
        if li and current_list is not None:
            item = li.group(1).strip().strip("\"\'")
            # Check if it's a phase dict-like item: name: "X", status: Y
            if "name:" in item and "status:" in item:
                name_m = re.search(r'name:\s*["\']?(.+?)["\']?\s*(?:,|$)', item)
                status_m = re.search(r'status:\s*["\']?(.+?)["\']?\s*(?:,|$|})', item)
                if name_m:
                    phase = {"name": name_m.group(1), "status": status_m.group(1) if status_m else "not-started"}
                    current_list.append(phase)
            elif current_key == "phases" and item.startswith("name:"):
                # Multi-line phase: "- name: X" with status on next line
                name_val = re.sub(r'^name:\s*', '', item).strip("\"\'")
                current_list.append({"name": name_val, "status": "not-started"})
            else:
                current_list.append(item)
            continue

        # Inline phase items with name:/status: on separate lines
        if current_key == "phases" and current_list is not None:
            name_line = re.match(r"\s+name:\s*(.+)", line)
            status_line = re.match(r"\s+status:\s*(.+)", line)
            if name_line:
                current_list.append({"name": name_line.group(1).strip("\"\'"), "status": "not-started"})
            elif status_line and current_list and isinstance(current_list[-1], dict):
                current_list[-1]["status"] = status_line.group(1).strip("\"\'")

    # Save final pending list
    if current_key and current_list is not None:
        result[current_key] = current_list

    return result


def _normalize_phases(phases: object, parent_status: str) -> list:
    """Normalize concise phases without overriding explicit phase mappings."""
    if not isinstance(phases, list):
        return []
    inferred_status = "done" if parent_status == "done" else "not-started"
    return [
        {"name": phase, "status": inferred_status} if isinstance(phase, str) else phase
        for phase in phases
        if isinstance(phase, (str, dict))
    ]


# ---------------------------------------------------------------------------
# Bold-text fallback parser
# ---------------------------------------------------------------------------

_BOLD_STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(.+)")
_BOLD_COMPLEXITY_RE = re.compile(r"\*\*Complexity:\*\*\s*(.+)")
_BOLD_ESTIMATED_RE = re.compile(r"\*\*Estimated:\*\*\s*(.+)")
_SESSIONS_NUM_RE = re.compile(r"(\d+)")
_CRITERIA_SECTION_RE = re.compile(r"^##\s+Success\s+Criteria", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)")


def _parse_bold_text(text: str) -> dict:
    """Parse spec metadata from bold-text markdown lines (legacy format)."""
    result: dict = {
        "status": "draft",
        "complexity": None,
        "estimated_sessions": None,
        "phases": [],
        "success_criteria": [],
        "files_touched": [],
    }

    lines = text.split("\n")
    in_criteria = False

    for i, line in enumerate(lines):
        # Status (first match only)
        m = _BOLD_STATUS_RE.search(line)
        if m and result["status"] == "draft":
            result["status"] = _normalize_status(m.group(1))
            continue

        # Complexity (first match only)
        m = _BOLD_COMPLEXITY_RE.search(line)
        if m and result["complexity"] is None:
            raw = m.group(1).strip()
            # Extract just the complexity word (Medium, High, Low, etc.)
            result["complexity"] = raw.split("—")[0].split("–")[0].strip().lower()
            continue

        # Estimated sessions (first match only)
        m = _BOLD_ESTIMATED_RE.search(line)
        if m and result["estimated_sessions"] is None:
            num_m = _SESSIONS_NUM_RE.search(m.group(1))
            if num_m:
                result["estimated_sessions"] = int(num_m.group(1))
            continue

        # Success criteria section
        if _CRITERIA_SECTION_RE.match(line):
            in_criteria = True
            continue

        # Next section ends criteria
        if in_criteria and line.startswith("## "):
            in_criteria = False
            continue

        # Collect bullets in criteria section
        if in_criteria:
            bm = _BULLET_RE.match(line)
            if bm:
                result["success_criteria"].append(bm.group(1).strip())

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_spec(spec_path: _Path | str) -> dict:
    """Parse a spec file and extract structured metadata.

    Tries YAML frontmatter first, falls back to bold-text regex parsing.

    Returns:
        {
            "name": "SPEC_DRIVEN_WORKFLOW",  # derived from filename
            "status": "ready",
            "complexity": "medium",
            "estimated_sessions": 2,
            "phases": [...],
            "success_criteria": [...],
            "files_touched": [],
            "raw_path": "/path/to/spec.md",
        }
    """
    spec_path = _Path(spec_path)
    text = spec_path.read_text(encoding="utf-8")

    # Derive name from filename
    name = spec_path.stem
    if name.endswith("_SPEC"):
        name = name[:-5]

    # Try YAML frontmatter first
    fm = _parse_frontmatter(text)
    if fm is not None:
        status = _normalize_status(fm.get("status", "draft"))
        complexity = fm.get("complexity")
        est = fm.get("estimated_sessions")
        if est is not None:
            try:
                est = int(est)
            except (ValueError, TypeError):
                est = None

        return {
            "name": name,
            "status": status,
            "complexity": complexity,
            "estimated_sessions": est,
            "phases": _normalize_phases(fm.get("phases", []), status),
            "success_criteria": fm.get("success_criteria", []),
            "files_touched": fm.get("files_touched", []),
            "raw_path": str(spec_path),
        }

    # Fallback to bold-text parsing
    parsed = _parse_bold_text(text)
    return {
        "name": name,
        "status": parsed["status"],
        "complexity": parsed["complexity"],
        "estimated_sessions": parsed["estimated_sessions"],
        "phases": parsed["phases"],
        "success_criteria": parsed["success_criteria"],
        "files_touched": parsed["files_touched"],
        "raw_path": str(spec_path),
    }


def list_specs(specs_dir: _Path | str) -> list[dict]:
    """List all specs in a directory with parsed metadata.

    Scans for *_SPEC.md files and parses each.
    """
    specs_dir = _Path(specs_dir)
    if not specs_dir.is_dir():
        return []

    results = []
    for path in sorted(specs_dir.glob("*_SPEC.md")):
        # Glob is case-insensitive on Windows — enforce case-sensitive stem match
        if not path.stem.endswith("_SPEC"):
            continue
        try:
            results.append(parse_spec(path))
        except (OSError, UnicodeDecodeError):
            # Skip unreadable files
            continue

    return results


def find_spec(specs_dir: _Path | str, query: str) -> dict | None:
    """Find a spec by name fragment (case-insensitive).

    Examples:
        find_spec(specs_dir, "convention") → CONVENTION_AUTOMATION_SPEC.md
        find_spec(specs_dir, "SPEC_DRIVEN") → SPEC_DRIVEN_WORKFLOW_SPEC.md
    """
    specs_dir = _Path(specs_dir)
    query_lower = query.lower().replace(" ", "_")

    for path in sorted(specs_dir.glob("*_SPEC.md")):
        if query_lower in path.stem.lower():
            return parse_spec(path)

    return None
