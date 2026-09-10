# task_execution_context

**Module:** `odibi_anchor.planning.task_execution_context`  
**Dependencies:** stdlib only (zero external deps)  
**Version:** 3.2

## Quick Start

```python
from odibi_anchor.planning import task_execution_context, quick_context

# Full structured context
ctx = task_execution_context(
    task="Build silver readings table.",
    goal="Clean, deduplicated readings.",
    mode="implementation",
)

# 80% case — human-readable brief
brief = quick_context("Merge bronze into silver, dedup by asset_id + date.")
```

## Functions

### `task_execution_context(task, **kwargs) → dict`

Generates a structured context dict with readiness scoring, a phased plan, bounded
task-relevant evidence questions, discovery gaps, handoff briefs, and mode-specific
guidance.

**Required:**
- `task` (str): What needs to be done.

**Key Optional:**
- `goal` (str): Desired outcome (+20 readiness points)
- `mode` (str): One of 9 modes — planning, implementation, testing, debugging, review, migration, handoff, decision, analysis
- `audience` (str): "self", "agent", "teammate"
- `acceptance_criteria` (list[str]): How to know it's done (+20 points)
- `constraints` (list[str]): Boundaries and rules (+10 points)
- `in_scope` / `out_of_scope` (list[str]): Scope clarity (+15 points)
- `background` (str): Context and history (+10 points)
- `artifacts` (list[dict]): Related files/tables (+10 points via resources)
- `evidence` (list[dict]): Gathered evidence (from previous passes)
- `risks` (list[str]): Known risks (+10 points)
- `deliverables` (list[str]): Expected outputs (+5 points)
- `current_state` (str): Where things stand now
- `requester` / `executor` (str): Ownership
- `priority` (str): Urgency level
- `options` (list[dict]): For decision mode

**Truncation Controls:**
- `max_plan_steps` (int): Cap plan length (default: 8)
- `max_items_per_section` (int): Cap list items (default: 10)
- `max_text_length` (int): Cap text fields (default: 500)

**Returns:** JSON-serializable dict

### `quick_context(task, **kwargs) → str`

Same inputs as `task_execution_context`, returns a formatted markdown brief string.

## Output Structure

```
ctx["status"]                → "ready" | "needs_detail" | "under_specified"
ctx["readiness"]["score"]    → 0-100
ctx["readiness"]["dimensions"] → per-dimension breakdown
ctx["plan"]                  → phased execution steps
ctx["context_plan"]["questions"] → bounded required/recommended evidence questions
ctx["discovery"]["evidence_gaps"] → what you don't know
ctx["discovery"]["recommended_context_generators"] → compatibility projection of candidate actions
ctx["handoff"]["prompt_brief"]    → agent-ready prompt
ctx["handoff"]["teammate_brief"]  → human-readable summary
ctx["hints"]["thinking_prompts"]  → mode-specific guidance
ctx["hints"]["anti_patterns"]     → what to avoid
ctx["hints"]["ready_to_ask_ai"]   → bool
ctx["metrics"]               → counts and stats
ctx["metadata"]["version"]   → "3.2"
```

Candidate action `availability` is `registered` when the dispatcher recognizes the
name and `unknown` when planning lacks dispatcher metadata. It does not establish
dependencies, credentials, permission, runtime support, collection success, or
evidence completeness. `called_this_session` means invocation only.

## Readiness Weights

| Dimension | Weight | What Fills It |
|-----------|--------|---------------|
| goal | 20 | `goal=` parameter |
| criteria | 20 | `acceptance_criteria=` |
| scope | 15 | `in_scope=` / `out_of_scope=` |
| background | 10 | `background=` |
| constraints | 10 | `constraints=` |
| resources | 10 | `artifacts=` |
| risks | 10 | `risks=` |
| deliverables | 5 | `deliverables=` |

## Modes

| Mode | Plan Phases | Use When |
|------|------------|----------|
| planning | discovery → scope → plan → validate | Starting a new task |
| implementation | setup → build → verify → deliver | Writing code |
| testing | setup → execute → validate → report | Testing artifacts |
| debugging | reproduce → isolate → fix → verify | Finding root cause |
| review | understand → assess → critique → recommend | Reviewing work |
| migration | inventory → plan → execute → verify | Moving/restructuring |
| handoff | summarize → document → brief → transfer | Passing work on |
| decision | frame → gather → evaluate → decide | Making a choice |
| analysis | scope → explore → synthesize → present | Analyzing data/code |
| greenfield | goal → create → test → verify | Creating new files/tools from scratch (skips spec, skill gates) |
