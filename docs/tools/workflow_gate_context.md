# workflow_gate_context

`workflow_gate_context` tracks actions taken vs obligations owed during a coding
session. It calculates your "verification debt" and tells you what MUST be done
before moving on. It answers:

```text
What obligations have my actions created?
Have I paid all my verification debts?
Am I safe to move to the next task?
```

## Public API

```python
from odibi_anchor.codebase import workflow_gate_context

def workflow_gate_context(
    root: str | Path,
    *,
    actions_taken: list[str],
    files_changed: list[str] | None = None,
    files_read: list[str] | None = None,
    workflow: str | None = None,
    obligations_paid: list[str] | None = None,
    verification_record: list[dict[str, str]] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `actions_taken` | list[str] | required | Actions performed (e.g., ["implement", "refactor"]) |
| `files_changed` | list[str] or None | None | Files modified during session |
| `files_read` | list[str] or None | None | Files read during session |
| `workflow` | str or None | None | Named workflow (auto-detects obligations) |
| `obligations_paid` | list[str] or None | None | Obligations already fulfilled |
| `verification_record` | list[dict] or None | None | Evidence of obligation fulfillment |
| `subject` | str or None | None | Human label |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "workflow_gate_context",
    "metrics": {"obligations_total": 4, "obligations_paid": 2,
                "obligations_unpaid": 2, "can_proceed": False},
    "obligations": [
        {"tool": "test_focus_context", "status": "paid", "evidence": "42 tests passed"},
        {"tool": "consistency_check_context", "status": "unpaid"},
    ],
    "suggested_next_actions": ["MUST: Run consistency_check_context", ...],
}
```

## When To Use

- To check what verification steps are still required before moving on
- At the end of a work block to verify all debts are paid
- When `risk_level: "high"` appears — stop and pay all MUST obligations
