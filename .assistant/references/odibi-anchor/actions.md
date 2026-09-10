# Odibi Anchor actions

> Deterministic distribution reference; it is not a discoverable skill.

Complete reference for anchor() actions that aren't covered by domain-specific skills.
Run `anchor("help")` for the full API overview, or `anchor("help", "action_name")` for one action.

## When to use this reference

- You need a tool but don't know which one exists
- `anchor("help")` is too terse — you need "when to use" guidance
- You're looking for session management, memory, or snapshot tools

## Session & Diagnostic Actions

These help you understand and manage the current session:

```python
# Step 1: See what happened this session (files changed, actions taken)
result = anchor("session_delta")

# Step 2: View session notes/log entries
result = anchor("session_log")

# Step 3: Check past gate audit results (compliance history)
result = anchor("audit_history")

# Step 4: Show the current context frame (task + session state summary)
result = anchor("frame")

# Step 5: List all registered skills and their load status
result = anchor("skills")

# Step 6: List all registered tools (custom extensions)
result = anchor("tools")

# Step 7: Show boot manifest for drift detection
result = anchor("manifest")

# `sync` is intentionally unavailable: external mutation is not authorized.
# Install/copy instructions only in a separately approved deployment phase.
```

| Action | When to Use | Signature |
|---|---|---|
| `session_delta` | End of session — see full change summary | `anchor("session_delta")` |
| `session_log` | View session notes you've logged | `anchor("session_log")` |
| `audit_history` | Check compliance trend across sessions | `anchor("audit_history")` |
| `frame` | Quick orient — current task + session state | `anchor("frame")` |
| `skills` | List available skills, check what's loaded | `anchor("skills")` |
| `tools` | List custom registered tools | `anchor("tools")` |
| `manifest` | Check boot manifest for environment drift | `anchor("manifest")` |
| `sync` | Unavailable under the current external-mutation boundary | Do not call; use a separately approved deployment process |

## Memory Curation Actions

Manage the memory database (`.agent_memory.db`):

Memory follows the seven-stage cycle **Observe → Encode → Retrieve → Apply → Evaluate →
Consolidate → Govern**. CoALA categories map working memory to task/session state, episodic
memory to structured-learning evidence, semantic memory to curated memory entries, and
procedural memory to distributed guidance. Problems, Specs, decisions, and work items
remain authority. Memory is advisory and never replaces verification.

```python
# Additional free-text retrieval is for an accepted task whose bounded context is insufficient
result = anchor("memory", query="schema migration", limit=10, offset=0)
# Continue with offset=result["metrics"]["next_offset"] while has_more is true.

# Task acceptance also performs bounded, task-aware retrieval. Apply and evaluate only
# when a selected memory actually influences work.
application = anchor("memory", "apply", selection_id="sel_...",
                 action="check schema compatibility", context={"task": "migration"})
evaluation = anchor("memory", "evaluate", application_id=application["application_id"],
                outcome="helpful", evidence={"check": "compatibility test passed"})

# Read-only forensic context restoration; never executes events or replays reasoning
replay = anchor("memory", "replay", task_window_id="tw_...", view="inspect")
verified = anchor("memory", "replay", task_window_id="tw_...", view="verify")
context = anchor("memory", "replay", task_window_id="tw_...", view="context")

# Diagnostics and a non-destructive migration plan
storage = anchor("memory", "storage", command="inspect")
plan = anchor("memory", "storage", command="plan", destination="/absolute/path")

# Compatibility-only confirmation call. This returns confirmation_blocked without
# changing candidate authority; use a governed promotion lane instead.
result = anchor("confirm", "entry_id_abc123")

# Owner-governed convention/preference: each transition requires a separate authenticated
# approval from the configured owner. The runtime derives identity, challenge, and receipt.
result = anchor("memory", "promotion", command="request_owner_activation",
            memory_id="entry_id_abc123", timeout_minutes=15)
result = anchor("memory", "promotion", command="request_owner_confirmation",
            memory_id="entry_id_abc123", timeout_minutes=15)

# Reject a memory entry (marks as stale/wrong)
result = anchor("reject", "entry_id_abc123")

# Archive old entries not accessed in N days
result = anchor("archive", max_unused_days=90)

# Export all memory entries to markdown (for backup/review)
result = anchor("export_md")

# Import memory from a markdown export
result = anchor("import_md")
```

| Action | When to Use | Signature |
|---|---|---|
| `memory` retrieve | Explicit semantic-memory search; ordinary calls remain compatible | `anchor("memory", query="...")` |
| `memory apply` | Record that a task selection materially informed an action | `anchor("memory", "apply", selection_id=..., action=..., context={...})` |
| `memory evaluate` | Attach evidence-backed feedback to an application | `anchor("memory", "evaluate", application_id=..., outcome="helpful|not_helpful|harmful|superseded", evidence={...})` |
| `memory replay` | Inspect/verify a task journal or restore a bounded forensic context package | `anchor("memory", "replay", task_window_id=..., view="inspect|verify|context")` |
| `memory storage inspect` | Diagnose profile, trust domain, path safety, schema, and backups without movement | `anchor("memory", "storage", command="inspect")` |
| `memory storage plan` | Produce a non-destructive migration plan; execution needs separate approval | `anchor("memory", "storage", command="plan", destination="/absolute/path")` |
| `memory promotion` | Promote a mechanically verified claim or request governed owner authority through configured Slack, a local Windows dialog, or a lower-assurance two-step Databricks in-session assertion | `anchor("memory", "promotion", command="request_owner_activation|request_owner_confirmation", memory_id=..., provider="databricks_in_session", in_session_approval="APPROVE <prepared-challenge>")`; on Databricks omit `in_session_approval` first, stop for the owner's new exact Genie reply, then rerun with that reply; omit `provider` for default Slack-first selection |
| `confirm` | Blocked legacy compatibility; does not promote | `anchor("confirm", "entry_id")` returns `confirmation_blocked` and points to governed promotion |
| `reject` | A memory entry is stale, wrong, or superseded | `anchor("reject", "entry_id")` |
| `archive` | Prune old unused entries to keep DB fast | `anchor("archive", max_unused_days=90)` |
| `export_md` | Backup memory to markdown for review/transfer | `anchor("export_md")` |
| `import_md` | Restore memory from a markdown export | `anchor("import_md")` |

Direct memory retrieval applies existing filters before relevance ranking, then returns the
ranked slice `[offset:offset + limit]`. Pagination metadata is available under `metrics` as
`total_matches`, `returned_count`, `offset`, `next_offset`, and `has_more`. At task acceptance,
pass `memory_limit=1..20` to override the default five bounded selections; each returned
selection still creates a disposition obligation.

Structured `learning triage` is the human-attested consolidation boundary. Publishing an
active lesson/watch projects one idempotent semantic candidate with lineage. Retrieval,
recurrence, application, evaluation, successful execution, and their counters never promote
memory.

Memory retrieval ranks the complete eligible project-scoped corpus before applying the
requested result limit. Immutable selections, applications, and evaluations supply retrieval
evidence; legacy mutable counters do not grant authority. `memory_tags`, `memory_stats`, and
memory diagnostics require the active project/trust scope. Corrections to active or confirmed
memories atomically append an authority withdrawal before terminalization; reactivation needs a
new governed owner decision. Gate-time typed verification includes a fair bounded project-local
sweep, so eligible structured-learning claims can progress without first winning retrieval.
Legacy counters are deprecated non-authoritative compatibility fields: `use_count`,
`surface_count`, `applied_count`, and `sessions_seen`. Immutable lifecycle selections,
applications, and evaluations are the authoritative usage telemetry.
The legacy memory co-occurrence tables remain readable for compatibility, but production does
not update or surface associations from them.
Promotion requires either a supported typed verifier or the two-step authenticated
owner flow above. Replay returns redacted evidence and explicit unavailable
evidence—not model state, chain of thought, credentials, writes, or external effects.

## Snapshot Actions

Capture and restore session state:

```python
# Save a workflow result to disk for later resumption
result = anchor("save_snap", workflow_result, "snapshots/bronze_fix.json")

# Load a previously saved snapshot
result = anchor("load_snap", "snapshots/bronze_fix.json")
```

| Action | When to Use | Signature |
|---|---|---|
| `save_snap` | Preserve expensive workflow result for cross-session use | `anchor("save_snap", result, "path.json")` |
| `load_snap` | Resume from a saved snapshot without re-running | `anchor("load_snap", "path.json")` |

## Composed Workflow Actions

High-level workflows that chain multiple tools:

```python
# Investigate a table: auto-chains profile_table → microscope → quality
result = anchor("investigate", "catalog.schema.table", columns=["amount", "status"])

# Investigate a DataFrame with custom rules
result = anchor("investigate", df, subject="orders", rules=[{"column": "id", "rule": "not_null"}])

# Chain: feed output of one workflow into another
result = anchor("chain", previous_result, target="evolve")
```

| Action | When to Use | Signature |
|---|---|---|
| `investigate` | Full table investigation (profile + quality + microscope) | `anchor("investigate", table_or_df, columns=[...])` |
| `chain` | Feed workflow output into next workflow step | `anchor("chain", result, target="action_name")` |

## File Tracking & Logging Actions

```python
# Log a session note (category + message)
result = anchor("log", "pipeline", "bronze orders loaded 6454 rows")
result = anchor("log", "decision", "Using LEFT JOIN instead of INNER to preserve all source rows")

# View the session log
result = anchor("session_log")
```

| Action | When to Use | Signature |
|---|---|---|
| `log` | Record a decision, observation, or milestone during work | `anchor("log", "category", "message")` |
| `session_log` | Review all notes logged this session | `anchor("session_log")` |

## Meta & Extension Actions

```python
# Get help on any action
result = anchor("help")                    # full API overview
result = anchor("help", "investigate")     # detailed signature for one action
result = anchor("help", "before a join")   # ask what to use for a problem

# Register a custom tool (advanced)
result = anchor("register_tool", name="my_checker", callable_path="my_module:check_func")
```

| Action | When to Use | Signature |
|---|---|---|
| `help` | Find the right tool, get signatures, explore API | `anchor("help")` or `anchor("help", "topic")` |
| `register_tool` | Add a custom callable to the dispatcher | `anchor("register_tool", name="...", callable_path="mod:func")` |

## Decision Table: "I want to..."

| I want to... | Use |
|---|---|
| See what I changed this session | `anchor("session_delta")` |
| Check my compliance history | `anchor("audit_history")` |
| Find the right tool for a task | `anchor("help", "problem description")` |
| Quick-orient on current task state | `anchor("frame")` |
| Save expensive result for later | `anchor("save_snap", result, path)` |
| Resume from where I left off | `anchor("load_snap", path)` |
| Clean up old memory entries | `anchor("archive", max_unused_days=90)` |
| Record that a task-selected memory helped | `memory apply` followed by evidence-backed `memory evaluate`; entry remains a candidate |
| Mark a memory entry as wrong | `anchor("reject", "entry_id")` |
| Record a decision mid-session | `anchor("log", "decision", "why I chose X")` |
| Review all session notes | `anchor("session_log")` |
| Run full table investigation | `anchor("investigate", table_name, columns=[...])` |
| Feed one workflow's output into another | `anchor("chain", result, target="next_action")` |
| List available skills | `anchor("skills")` |
| Check for environment drift | `anchor("manifest")` |
| Install instructions outside the source tree | Separate approved deployment process (`anchor("sync")` is blocked) |
| Backup memory to markdown | `anchor("export_md")` |
| Restore memory from backup | `anchor("import_md")` |
| Add a custom tool to dispatcher | `anchor("register_tool", name="...", callable_path="...")` |
| Check registered custom tools | `anchor("tools")` |

## What NOT to Do

| Anti-pattern | Why | Use instead |
|---|---|---|
| Write manual session summaries | `session_delta` does this automatically | `anchor("session_delta")` |
| Manually track what files you changed | Session state tracks this | `anchor("session_delta")` or `anchor("session_files")` |
| Export memory with Python file I/O | Loses metadata, format drift | `anchor("export_md")` |
| Skip `anchor("audit_history")` at session start | Mandatory sequence step 3 | NEVER skip — RuntimeError |
| Call `anchor("help")` without checking action documentation | This reference has richer "when to use" guidance | Consult this action reference first |
| NEVER log to print() for important decisions | Logs are ephemeral, session_log persists | `anchor("log", "decision", "...")` |
| NEVER guess at what tools exist | anchor("help") + anchor("tools") show everything | Use discovery tools |

## Related distribution references

| Reference | Covers |
|---|---|
| `quick-reference.md` | Concise action and rule lookup |
| `workflow.md` | Core workflow and action sequence |
| `../lifecycle/thread-discipline.md` | Durable handoffs and thread boundaries |
| `../lifecycle/compliance-gates.md` | Gate, checkpoint, and learning protocol |
