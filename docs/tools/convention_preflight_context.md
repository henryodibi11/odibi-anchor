# convention_preflight_context

`convention_preflight_context` produces proactive convention guidance BEFORE
writing code. Unlike `consistency_check_context` (reactive — detects violations
after code is written), this tool tells the agent what rules apply BEFORE the
first line is written.

It answers:

```text
What conventions must I follow for this type of change?
What does the standard API signature look like?
What companion files do I need (render function, tests, docs, exports)?
What existing patterns should I match?
```

Designed to prevent "agent violates conventions it already knows about" by
embedding the checklist directly into the pre-write context.

## Public API

```python
from odibi_anchor.codebase import convention_preflight_context

def convention_preflight_context(
    root: str | Path,
    *,
    action: str = "new_function",
    target_file: str | None = None,
    function_name: str | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `action` | str | `"new_function"` | Type of change planned (see table below) |
| `target_file` | str | None | Relative path to file being created/modified |
| `function_name` | str | None | Name of function being created/modified |
| `subject` | str | None | Human label. Defaults to function_name or file stem |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Action Types

| Action | When to use |
|--------|------------|
| `new_tool` | Creating a new context generator (strictest: render, contract, exports) |
| `new_function` | Creating any new public function |
| `new_module` | Creating a new .py file/module |
| `modify_function` | Changing an existing public function |
| `add_parameter` | Adding a parameter to an existing function |
| `refactor` | Restructuring code without changing behavior |

## Output Shape

```python
{
    "kind": "convention_preflight_context",
    "subject": "anomaly_detection_context",
    "summary": "Preflight for new_tool: 9 convention(s) to follow. Target: anomaly_detection_context.",
    "metrics": {
        "checklist_item_count": 9,
        "action": "new_tool",
        "patterns_scanned": 52,
        "has_target_file": False,
    },
    "checklist": [
        {"rule": "Function must be standalone — no classes, no framework abstractions",
         "severity": "blocker", "category": "architecture"},
        {"rule": "Must accept `output_format='dict'|'markdown'` parameter",
         "severity": "blocker", "category": "api"},
        {"rule": "Must return standard contract keys: kind, subject, summary, ...",
         "severity": "blocker", "category": "contract"},
        {"rule": "Must have paired render function: render_anomaly_detection_report(ctx)",
         "severity": "blocker", "category": "api"},
        ...
    ],
    "examples": {
        "existing_signatures": [
            {"name": "quality_gate_context", "signature": "def quality_gate_context(df, *, keys, ...)"},
        ],
        "suggested_signature": "def anomaly_detection_context(df, *, subject='dataframe', engine='auto', output_format='dict') -> dict[str, Any] | str",
        "suggested_render": "def render_anomaly_detection_report(ctx: dict[str, Any]) -> str",
        "return_contract_keys": ["kind", "subject", "summary", "metrics", "findings", "risks", "samples", "suggested_next_actions"],
    },
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

## Checklist Severity Levels

| Severity | Meaning |
|----------|---------|
| `blocker` | MUST follow — consistency_check will flag violations |
| `warning` | SHOULD follow — improves quality but not strictly enforced |

## Usage Examples

### Before building a new context generator

```python
ctx = convention_preflight_context(
    "/path/to/odibi_anchor",
    action="new_tool",
    function_name="anomaly_detection_context",
)

# Read checklist before writing any code
for item in ctx["checklist"]:
    icon = "❌" if item["severity"] == "blocker" else "⚠️"
    print(f"{icon} {item['rule']}")

# Use examples for consistent API shape
print(ctx["examples"]["suggested_signature"])
```

### Before adding a parameter

```python
ctx = convention_preflight_context(
    "/path/to/project",
    action="add_parameter",
    target_file="src/my_pkg/validation/quality_gate_context.py",
    function_name="quality_gate_context",
)
# Checklist: must be keyword-only, must have default, update docstring, add tests
```

### Before refactoring

```python
ctx = convention_preflight_context(
    "/path/to/project",
    action="refactor",
)
# Checklist: no behavior changes, run consistency_check after, all tests pass
```

### Markdown for agent prompt injection

```python
report = convention_preflight_context(
    root, action="new_tool",
    function_name="drift_context",
    output_format="markdown",
)
# Inject into agent prompt before code generation
```

## When to Use

| Situation | Use convention_preflight_context |
|-----------|-------------------------------|
| About to write a new tool | `action="new_tool"` |
| Adding a parameter to existing function | `action="add_parameter"` |
| Creating a new module | `action="new_module"` |
| Before any refactoring | `action="refactor"` |

## Workflow Integration

```text
Before writing code:
  1. convention_preflight_context(root, action=..., function_name=...)
  2. Read checklist → follow conventions
  3. Write code
  4. consistency_check_context(root) → verify no drift

The two tools are complementary:
  - preflight = proactive guidance (before)
  - consistency_check = reactive enforcement (after)
```

## Relationship to Other Tools

| Tool | Role |
|------|------|
| `convention_preflight_context` | Proactive — what to do |
| `consistency_check_context` | Reactive — what went wrong |
| `codebase_map_context` | Navigation — what exists |
| `change_impact_context` | Risk — what breaks |
