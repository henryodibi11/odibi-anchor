# session_snapshot_context

`session_snapshot_context` captures the current state of a working session — not what was done (narrative), but what the world looks like right now (state) plus the reasoning behind decisions.

Think of it as a **save-game for AI coding sessions**: auto-scan the codebase state, package it with decisions/reasoning, save to disk, load next session.

## Public API

```python
def session_snapshot_context(
    root: str | Path,
    *,
    subject: str | None = None,
    decisions: list[str] | None = None,
    rejected_alternatives: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_steps: list[str] | None = None,
    session_notes: str | None = None,
    test_state: dict[str, int] | None = None,
    previous_snapshot: dict[str, Any] | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
```

### Helpers

```python
def save_snapshot(snapshot: dict, path: str | Path) -> None:
    """Save snapshot dict to JSON file."""

def load_snapshot(path: str | Path) -> dict | None:
    """Load previous snapshot. Returns None if file doesn't exist."""

def render_session_snapshot_report(ctx: dict) -> str:
    """Render snapshot as markdown report."""
```

## Parameters

| Parameter | Type | Default | Purpose |
| --- | --- | --- | --- |
| `root` | str/Path | required | Project directory to scan |
| `subject` | str | dir name | Human label for the project |
| `decisions` | list[str] | [] | Key decisions made and WHY |
| `rejected_alternatives` | list[str] | [] | Approaches tried and rejected |
| `open_questions` | list[str] | [] | Unresolved threads to carry forward |
| `next_steps` | list[str] | [] | What to do next session |
| `session_notes` | str | None | Free-form notes |
| `test_state` | dict | None | {"passed": N, "failed": N, "skipped": N} |
| `previous_snapshot` | dict | None | Prior snapshot for diff |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "session_snapshot_context",
    "subject": "odibi_anchor",
    "summary": "odibi_anchor: 82 files, 28053 lines. 416 tests passing.",
    "timestamp": "2026-05-11T04:01:39+0000",
    "metrics": {
        "file_count": 82,
        "total_lines": 28053,
        "total_size_bytes": 972521,
        "py_file_count": 46,
        "md_file_count": 35,
    },
    "file_states": {
        "src/module.py": {"lines": 200, "size": 5000, "hash": "a3f2b1c9d4e5"},
        ...
    },
    "test_state": {"passed": 416, "skipped": 1, "failed": 0},
    "changes_since": {           # Only when previous_snapshot provided
        "added": ["new_file.py"],
        "removed": [],
        "modified": ["changed_file.py"],
        "unchanged_count": 75,
    },
    "decisions": ["Used stdlib AST for zero deps"],
    "rejected_alternatives": ["tree-sitter: too heavy"],
    "open_questions": ["Should X get output_format?"],
    "next_steps": ["Build Y", "Update docs"],
    "session_notes": "...",
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

## How It Works

1. **Auto-scan**: Walks the project tree, tracking `.py`, `.md`, `.yaml`, `.yml`, `.toml`, `.cfg`, `.txt`, `.json` files. Computes SHA-256 hash (12 chars), line count, byte size for each.
2. **Skip**: Hidden directories (`.git`), `__pycache__`, `node_modules`.
3. **Package reasoning**: Stores decisions, rejected alternatives, open questions, next steps verbatim.
4. **Diff**: If `previous_snapshot` provided, compares file hashes to identify added/modified/removed files.
5. **Persist**: `save_snapshot()` writes JSON. `load_snapshot()` reads it back.

## Typical Workflow

### End of Session (agent saves state)

```python
from odibi_anchor.codebase import session_snapshot_context, save_snapshot

snapshot = session_snapshot_context(
    "/path/to/project",
    decisions=[
        "Used stdlib AST for zero deps",
        "Chose dict output over dataclass for JSON compat",
    ],
    rejected_alternatives=[
        "tree-sitter: adds C dependency, overkill for Python-only",
    ],
    open_questions=[
        "Should task_execution_context get output_format?",
    ],
    next_steps=[
        "Build code_pattern_context",
        "Update quickstart docs",
    ],
    test_state={"passed": 457, "skipped": 1, "failed": 0},
)
save_snapshot(snapshot, "/path/to/project/.session_snapshot.json")
```

### Start of Next Session (agent loads state)

```python
from odibi_anchor.codebase import session_snapshot_context, load_snapshot

prev = load_snapshot("/path/to/project/.session_snapshot.json")
# Immediately know: what exists, decisions made, what was rejected, what's next

# Optionally capture current state with diff
current = session_snapshot_context(
    "/path/to/project",
    previous_snapshot=prev,
    test_state={"passed": 460, "skipped": 1, "failed": 0},
)
# current["changes_since"] shows what changed between sessions
```

### Quick Check (no persistence)

```python
snapshot = session_snapshot_context("/path/to/project")
print(f"Files: {snapshot['metrics']['file_count']}")
print(f"Lines: {snapshot['metrics']['total_lines']}")
```

## Key Differences from Session Summary

| Session Summary | Session Snapshot |
| --- | --- |
| Written *about* the session | Written *for* the next session |
| Narrative (what happened) | State (what exists now) |
| History | Save file |
| Good for humans reading later | Good for agents resuming work |
| No structure guarantee | Standard context contract |
| Manual | Auto-scanned + structured |

## Design Decisions

- **Stdlib only**: hashlib, json, os, pathlib, time. Zero external dependencies.
- **12-char hash**: SHA-256 truncated. Collision risk negligible for <1000 files.
- **Tracked extensions**: Only code/config/docs. Binary files, CSVs, images excluded.
- **Save/load are separate**: The tool doesn't auto-persist. Caller decides when/where.
- **`previous_snapshot` is optional**: No state dependency. Works standalone.

## Testing

41 tests covering:
- Output contract (kind, subject, summary, metrics)
- Auto-scan (file discovery, hash computation, pycache/hidden exclusion)
- Reasoning inputs (decisions, rejected, questions, notes, test_state)
- Snapshot diffing (added, modified, removed, unchanged_count)
- Save/load (roundtrip, nonexistent file, parent dir creation)
- Output format dispatch (dict, markdown, invalid raises)
- Render function (all sections, failing tests, changes)
- Dog-food against odibi_anchor itself
