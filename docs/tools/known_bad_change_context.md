# known_bad_change_context

`known_bad_change_context` is a pre-edit guardrail that checks a proposed change
against project memory (gotchas, failure patterns) and returns a warn/block
signal BEFORE the edit is applied. It answers:

```text
Has this type of change caused failures before?
Should I proceed, or try a different approach?
```

## Public API

```python
from odibi_anchor.codebase import known_bad_change_context

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
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `proposed_diff` | str or None | None | Unified diff of the proposed change |
| `changed_files` | list[str] or None | None | Files being modified |
| `action` | str or None | None | Action type (e.g., "rename_parameter") |
| `task_type` | str or None | None | Task context (e.g., "refactor") |
| `error_text` | str or None | None | Error that triggered this change |
| `memory_path` | str or None | None | Override path to memory JSONL |
| `patterns_path` | str or None | None | Path to failure_patterns.yaml |
| `subject` | str or None | None | Human label |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "known_bad_change_context",
    "metrics": {"status": "block", "memories_checked": 5, "memories_matched": 1,
                "blocking_count": 1, "warning_count": 0},
    "matched_memories": [{"id": "m0001", "type": "failure_pattern", ...}],
    "matched_patterns": [],
    "risks": ["Known failure [m0001] (confidence=0.9, confirmed 3x): ..."],
    "suggested_next_actions": ["MUST: Do NOT apply this change. ..."],
}
```

## When To Use

- Before any rename/refactor operation to check for known pitfalls
- As the first step in the safe_change_context pipeline
- When an action matches patterns that caused past failures
