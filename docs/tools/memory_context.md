# memory_context

`memory_context` queries the project's structured SQLite memory (`.agent_memory.db`) to
retrieve relevant past learnings, gotchas, decisions, and failure patterns. It answers:

```text
What do I already know about these files/this task?
Are there known pitfalls for this type of change?
What patterns has this project established?
```

Replaces manually reading `.agent_memory.md` — returns only entries relevant to
the current task, scored and ranked by relevance.

## Public API

```python
from odibi_anchor.codebase import memory_context

def memory_context(
    root: str | Path,
    *,
    files_involved: list[str] | None = None,
    tags: list[str] | None = None,
    error_text: str | None = None,
    task_type: str | None = None,
    entry_type: str | None = None,
    query: str | None = None,
    limit: int = 10,
    include_archived: bool = False,
    include_retired: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root used to resolve the active memory store |
| `files_involved` | list[str] or None | None | Files being worked on (for glob matching) |
| `tags` | list[str] or None | None | Tags to match against memory entries |
| `error_text` | str or None | None | Error text to match against memory content |
| `task_type` | str or None | None | Task type (e.g., "refactor", "debug") |
| `entry_type` | str or None | None | Filter to specific type (gotcha, decision, pattern, failure_pattern) |
| `query` | str or None | None | Free-text search query |
| `limit` | int | 10 | Maximum entries to return |
| `include_archived` | bool | False | Include archived (stale) entries |
| `include_retired` | bool | False | Include retired entries |
| `subject` | str or None | None | Human label for output |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "memory_context",
    "subject": "project_name",
    "summary": "5 relevant memories (3 gotchas, 2 patterns)",
    "metrics": {
        "total_entries": 15,
        "returned_entries": 5,
        "has_results": True,
    },
    "entries": [
        {"id": "m0001", "type": "gotcha", "content": "...", "tags": [...], ...},
    ],
    "findings": [...],
    "risks": [...],
    "samples": {},
    "suggested_next_actions": [...],
}
```

## Example Usage

```python
from odibi_anchor.codebase import memory_context

ctx = memory_context(
    "/path/to/project",
    files_involved=["src/module.py"],
    task_type="refactor",
)
for entry in ctx["entries"]:
    print(f"[{entry['id']}] {entry['content']}")
```

## When To Use

- After a substantive task is accepted, review its bounded task-selected context before edits
- Query further only when that accepted task context is insufficient for a risky change or debug
- Use deep/tag-wide inspection only for an explicitly authorized memory-governance audit

Do not manufacture a memory query for trivial or read-only work without durable task context.
Deep or full-store inspection is reserved for explicitly authorized memory-governance work.

## Dispatcher lifecycle

The dispatcher wraps retrieval in the seven-stage cycle **Observe → Encode → Retrieve →
Apply → Evaluate → Consolidate → Govern**. Existing queries remain compatible for an
accepted task whose bounded context is insufficient:

```python
memories = anchor("memory", query="schema migration")
```

An accepted `anchor("task")` result may also contain bounded `memory_context` selections chosen
automatically from its description, goal, profile, linked Problem/Spec, project, and
repository scope. Before source edits, review only this bounded set and acknowledge every
selected memory ID with status, source/provenance, scope, and reason—or explicitly report no
selections. Selection records are advisory exposure only; do not scan the full store or force
irrelevant advice. When a selection actually influences work, record and evaluate it explicitly:

```python
application = anchor("memory", "apply", selection_id="sel_...",
                 action="validate compatibility", context={"task_window_id": "tw_..."})
evaluation = anchor("memory", "evaluate", application_id=application["application_id"],
                outcome="helpful", evidence={"test": "compatibility check passed"})
```

Evaluation outcomes are `helpful`, `not_helpful`, `harmful`, or `superseded`; gate success
alone is not feedback. Structured `learning triage` is the human-attested, idempotent
episodic-to-semantic projection boundary.

CoALA working memory is task/session state, episodic memory is structured-learning evidence,
semantic memory is curated entries, and procedural memory is immutable skills/references.
Problems, Specs, decisions, and work items—not memory—grant authority. Always verify memory
against current evidence.

Every selection needs a truthful disposition before terminal closure. If bounded retrieval
is unavailable, retain that boundary and follow the existing fail-closed/degraded policy
without widening scope or inventing evidence.

For read-only forensics, use `anchor("memory", "replay", task_window_id=...,
view="inspect|verify|context")`. This restores recorded, redacted context; it neither executes
actions nor replays chain-of-thought. For governance, use
`anchor("memory", "storage", command="inspect")` or produce a non-destructive migration plan with
`anchor("memory", "storage", command="plan", destination="/absolute/path")`. Planning does not
move data; execution and cross-trust-domain transfer require separate approval.
