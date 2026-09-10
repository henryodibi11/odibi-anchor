# Documentation workflow

> Preserved technique source for the native owner.

When documentation is the deliverable, not a side effect of code changes. Covers READMEs,
architecture decision records, runbooks, API documentation, and module guides.

## Applicable assurance overlay

When structured selection activates `accessibility.user-interface`, `architecture.change`, or
operational runbook evidence under `operations.reliability`, use
`.assistant/references/assurance/standards-overlays.md` for the applicable outcomes and
result-evidence contract. This workflow remains the procedure owner; the pointer creates no
obligation by itself.

## When to Use This Skill

| Request | Use this skill? |
|---|---|
| "Write a README for this module" | ✅ Yes |
| "Document the pipeline architecture" | ✅ Yes |
| "Create a runbook for this process" | ✅ Yes |
| "Add docstrings to this code" | ❌ No — follow the repository's code standards during the code change |
| "Add comments explaining the logic" | ❌ No — inline with code changes |

## Core Principles

1. **Write for the reader, not the writer** — the person reading your docs doesn't have your context
2. **Accuracy over completeness** — incomplete but accurate docs > comprehensive but wrong docs
3. **Show, don't just tell** — code examples > abstract descriptions
4. **Keep it maintainable** — docs that can't be kept current are worse than no docs
5. **One source of truth** — don't duplicate information, reference it

## Document Types and Templates

### README.md

For modules, packages, or projects:

```markdown
# Module Name

One-sentence description of what this module does and why it exists.

## Quick Start

\`\`\`python
from module import main_function

result = main_function(input_data)
\`\`\`

## What It Does

[2-3 paragraphs: purpose, approach, key design decisions]

## API Reference

### `main_function(param1, param2, *, option=default)`

[Description]

**Parameters:**
- `param1` (Type) — [what it is]
- `param2` (Type) — [what it is]
- `option` (Type, default=X) — [what it controls]

**Returns:** Type — [what it contains]

**Example:**
\`\`\`python
result = main_function("input", 42, option=True)
\`\`\`

## Architecture

[How the pieces fit together — diagram if >3 components]

## Configuration

[What's configurable, where config lives, defaults]

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| [Common error] | [Why it happens] | [How to fix] |
```

### Architecture Decision Record (ADR)

For recording WHY a decision was made:

```markdown
# ADR-NNN: [Decision Title]

**Date:** YYYY-MM-DD
**Status:** Accepted | Superseded by ADR-NNN | Deprecated

## Context

[What situation prompted this decision? What problem are we solving?]

## Decision

[What did we decide? Be specific.]

## Consequences

### Positive
- [Benefit 1]
- [Benefit 2]

### Negative
- [Trade-off 1]
- [Trade-off 2]

### Risks
- [Risk and mitigation]

## Alternatives Considered

### [Alternative A]
Rejected because: [reason]

### [Alternative B]
Rejected because: [reason]
```

### Runbook

For operational procedures:

```markdown
# Runbook: [Process Name]

**Owner:** [Team/person]
**Frequency:** [Daily/weekly/on-demand/on-failure]
**Last verified:** YYYY-MM-DD

## Prerequisites

- [ ] [Access/permission needed]
- [ ] [Tool/system available]

## Procedure

### Step 1: [Action]

\`\`\`bash
[exact command]
\`\`\`

**Expected output:** [what you should see]
**If it fails:** [what to do — link to troubleshooting]

### Step 2: [Action]

[continue...]

## Verification

How to confirm the procedure succeeded:
- [ ] [Check 1]
- [ ] [Check 2]

## Rollback

If something goes wrong:
1. [Rollback step 1]
2. [Rollback step 2]

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| [Error message] | [Why] | [What to do] |
```

### Pipeline Documentation

For data pipelines:

```markdown
# Pipeline: [Pipeline Name]

## Overview

**Source:** [where data comes from]
**Target:** [where data goes]
**Frequency:** [how often it runs]
**SLA:** [when it must complete by]

## Data Flow

\`\`\`
[Source] → [Bronze] → [Silver] → [Gold]
   ↓          ↓          ↓          ↓
[raw file] [string cols] [typed, deduped] [aggregated]
\`\`\`

## Schema

### Source
| Column | Type | Description | Nullable |
|---|---|---|---|

### Target
| Column | Type | Description | Nullable |
|---|---|---|---|

## Business Rules

1. [Rule: e.g., "Dedup on project_id + queue_date, keep latest by file_modified_at"]
2. [Rule: e.g., "Exclude projects with status = 'Withdrawn'"]

## Key Decisions

| Decision | Rationale |
|---|---|
| [e.g., "Overwrite mode for gold"] | [e.g., "Table is small, full refresh is simpler"] |

## Monitoring

- **Row count alert:** [threshold]
- **Freshness alert:** [max age]
- **Known failure modes:** [what breaks and why]
```

## Writing Process

### 1. Identify the Audience

| Audience | What they need | Tone |
|---|---|---|
| New team member | Concepts, context, how to get started | Explanatory, patient |
| Experienced developer | API reference, edge cases, decisions | Concise, technical |
| Operator/on-call | Step-by-step procedures, troubleshooting | Precise, no ambiguity |
| Future you/agent | What's non-obvious, why things are the way they are | Specific, decision-focused |

### 2. Gather Information

```python
# Understand the code you're documenting
anchor("map")  # Structure
anchor("map", focus_file="target.py")  # Specific module

# Check existing docs
# Read any existing README, docstrings, comments
```

Review the accepted task's bounded `memory_context` for advisory documentation history before
drafting. Use an additional scoped query only when that bounded context is materially insufficient;
do not default to a full-store/tag scan.

### 3. Write Draft

Follow the appropriate template above. Key rules:
- **Lead with purpose** — first sentence answers "what does this do?"
- **Include working examples** — copy-pasteable code that actually runs
- **Document the WHY** — code shows WHAT, docs explain WHY
- **Use tables** for structured information (parameters, config, troubleshooting)
- **Keep sections short** — if a section is >20 lines, split it

### 4. Verify Accuracy

- **Run every code example** — if it doesn't work, fix it or remove it
- **Check function signatures** against actual code
- **Verify file paths** and module names
- **Test procedures** — follow your own runbook

## Anti-Patterns

| Don't | Do instead |
|---|---|
| Document implementation details that change | Document contracts and interfaces |
| Write "see code for details" | Either document it or don't — don't punt |
| Copy-paste code without context | Explain what the example demonstrates |
| Write for yourself right now | Write for someone with no context |
| Document everything at the same depth | More detail for complex/non-obvious parts |
| Let docs drift from code | Include doc updates in code change checklists |

## Integration with odibi-anchor

```python
# Plan the documentation task
anchor("task", "write README for transform module",
    goal="create comprehensive README covering API, architecture, and examples",
    mode="implementation")

# After writing docs
anchor("touched", "docs/README.md")
anchor("gate")
observation = anchor("learning", "capture", observation_type="reusable_practice", ...)
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[observation["item"]["item_id"]])
```
