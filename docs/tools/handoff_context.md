# handoff_context

`handoff_context` generates structured cross-thread handoff packets that
preserve decisions, blockers, evidence chains, and continuation instructions
across agent thread boundaries.

It answers:

```text
What is the current state of the work?
What decisions were made and why?
What evidence was gathered?
What should the next agent/human do first?
What can be skipped (already done)?
```

Designed to eliminate the "40% signal loss" problem — when work context is
compressed into narrative text during thread transitions, structure is lost.
This tool produces a machine-readable handoff that the receiving agent can
parse immediately.

## Public API

```python
from odibi_anchor.planning import handoff_context

def handoff_context(
    task: str,
    *,
    state: str = "in_progress",
    goal: str | None = None,
    decisions: list[str] | None = None,
    rejected_alternatives: list[str] | None = None,
    blockers: list[str] | None = None,
    evidence_chain: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    next_action: str | None = None,
    context_needed: list[str] | None = None,
    skip: list[str] | None = None,
    open_questions: list[str] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `task` | str | required | What the work is about (one sentence) |
| `state` | str | `"in_progress"` | Current state: `not_started`, `in_progress`, `blocked`, `ready_for_review`, `complete` |
| `goal` | str | None | Desired outcome |
| `decisions` | list[str] | None | Key decisions made and why |
| `rejected_alternatives` | list[str] | None | Approaches tried and rejected |
| `blockers` | list[str] | None | Current blockers with resolution path |
| `evidence_chain` | list[dict] | None | Compressed tool outputs (each: `tool`, `summary`, opt `status`) |
| `artifacts` | list[dict] | None | File paths and roles (each: `path`, `role`, opt `note`) |
| `next_action` | str | None | The exact next step the receiver should take |
| `context_needed` | list[str] | None | What the receiver needs to load/read first |
| `skip` | list[str] | None | What's already done and should NOT be repeated |
| `open_questions` | list[str] | None | Unresolved questions to carry forward |
| `subject` | str | None | Human label. Defaults to first 50 chars of task |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Output Shape

```python
{
    "kind": "handoff_context",
    "subject": "Merge bronze pipeline",
    "summary": "Merge bronze pipeline: in progress. 1 evidence step(s). next: Run validation.",
    "state": "in_progress",
    "task": "Merge bronze pipeline into silver",
    "goal": "No data loss, no key violations",
    "metrics": {
        "decision_count": 2,
        "blocker_count": 0,
        "evidence_step_count": 1,
        "artifact_count": 1,
        "open_question_count": 0,
        "skip_count": 1,
        "is_blocked": False,
        "is_actionable": True,
    },
    "decisions": ["Used SCD2 for customer dim", "Chose hash-based dedup"],
    "rejected_alternatives": ["Manual window dedup — too fragile"],
    "blockers": [],
    "evidence_chain": [
        {"tool": "quality_gate_context", "status": "pass", "summary": "Write-safe, 0 blockers"},
    ],
    "artifacts": [
        {"path": "notebooks/bronze_to_silver.py", "role": "source", "note": "Main transform"},
    ],
    "continuation": {
        "next_action": "Run validation_summary_context on merged output",
        "context_needed": ["target schema", "SCD2 history table"],
        "skip": ["Source profiling — already done"],
    },
    "open_questions": [],
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

## Usage Examples

### End-of-session handoff

```python
ctx = handoff_context(
    "Build silver pipeline for queue positions",
    state="in_progress",
    goal="Typed silver table with snapshot grain validated",
    decisions=[
        "Grain is [application_id, snapshot_year, snapshot_month]",
        "Used SCD2 pattern for historical tracking",
    ],
    evidence_chain=[
        {"tool": "exploration_context", "status": "done",
         "summary": "290K rows, grain verified unique"},
        {"tool": "quality_gate_context", "status": "pass",
         "summary": "Write-safe, 0 blockers"},
    ],
    artifacts=[
        {"path": "silver/queue_positions.py", "role": "source"},
        {"path": "tests/test_queue_positions.py", "role": "test"},
    ],
    next_action="Run diff_tables_by_key against previous load",
    skip=["Source profiling", "Schema exploration"],
)
```

### Blocked handoff

```python
ctx = handoff_context(
    "Migrate legacy pipeline to odibi",
    state="blocked",
    blockers=["Source schema not finalized — DBA team ETA Friday"],
    decisions=["Will use merge_into pattern once schema is final"],
    next_action="Resume after schema freeze is confirmed",
    open_questions=["Should we backfill historical data?"],
)
```

### Markdown for human review

```python
report = handoff_context(
    "Quarterly data refresh",
    state="ready_for_review",
    output_format="markdown",
)
```

## When to Use

| Situation | Use handoff_context |
|-----------|-------------------|
| Ending a long session | Always — preserves decisions for next session |
| Thread context limit approaching | Compress evidence chain before continuing |
| Handing to another agent/thread | Include full continuation block |
| Blocked on external dependency | Record state + blockers + resume instructions |
| Work complete, needs review | State = "ready_for_review" with artifacts |

## Relationship to Other Tools

- **session_snapshot_context** captures file state + reasoning at project level
- **handoff_context** captures task state + continuation at work-item level
- Use both: snapshot for project continuity, handoff for task continuity
