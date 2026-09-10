# Durable memory and learning

> Preserved durable handoff and checkpoint technique.

How to capture supported reusable observations without manufacturing learning. Load for an
explicit learning-capture or handoff task; routine work follows the global closure contract.

## Learning firewall

Observations, recurrence, co-occurrence, exposure, and attention signals are evidence or
bookkeeping. They are not approval, priority, backlog, a Problem or Spec, policy,
permission, scope, or implementation authority. Only the explicit human-attested
lifecycle may derive or dispose of a Lesson, Watch item, or Improvement candidate.
A candidate is not approved work. `nothing_reusable_learned` is a valid successful
assessment and creates no synthetic learning item.

## What Warrants a Memory Entry

Save **decision points**, not routine actions. If it wouldn't save a future session at least 2 minutes of work, don't save it.

| Type | When to use | Value signal |
|---|---|---|
| `gotcha` | Non-obvious pitfall that would waste time if forgotten | **Highest** — prevents repeated mistakes |
| `failure_pattern` | Exact error + root cause + what NOT to try | High — eliminates dead-end debugging |
| `decision` | Architectural choice with rationale and alternatives rejected | High — prevents re-debating settled questions |
| `convention` | Established rule for how things should be done | Medium — enforces consistency |
| `pattern` | Reusable approach that worked | Medium — accelerates future implementation |
| `discovery` | Novel insight about codebase, data, or tooling | Medium — captures tribal knowledge |
| `preference` | User preference for how things should work | Low-medium — but critical for UX consistency |

---

## Quality Standards

**Minimum content length: 30 characters.** Must be specific and actionable.

### Good vs Bad Examples

| Type | ❌ Bad | ✅ Good |
|---|---|---|
| `gotcha` | "fixed the bug" | "BUG: candidate fetch mutated authority telemetry. Fix: record only final surfaced IDs and confirm explicitly." |
| `failure_pattern` | "test passed" | "ERROR: `KeyError: 'project_id'` in `silver_queue.py:45` — caused by upstream schema change (PJM renamed column to `proj_id`). Do NOT add try/except, fix the column mapping in bronze→silver" |
| `decision` | "updated the file" | "Chose `local_checkpoint` over `cache` for transform pipeline because 12-step chain exceeded lineage limit. Rejected `none` — rollback needed for QA. Trade-off: slower checkpoint but truncated lineage" |
| `convention` | "use good names" | "All transformer functions must accept `df` as first param and return `DataFrame` — df-in/df-out contract. No side effects (no writes, no state mutation)" |
| `pattern` | "used a window function" | "Dedup pattern for ISO queue data: `ROW_NUMBER() OVER (PARTITION BY project_id, queue_date ORDER BY file_modified_at DESC, _row_hash)` — tiebreaker `_row_hash` prevents non-deterministic ordering" |
| `discovery` | "learned something" | "Delta table `analytics_dev.bronze.queue_pjm` has OPTIMIZE ZORDER on `project_id` — point lookups are fast but full scans bypass the index. Use filter pushdown on `project_id` for 10x speedup" |
| `preference` | "user likes X" | "User prefers concise planning context for bounded single-file fixes" |

### Content Must Include

1. **What happened** — the specific situation or error
2. **Why** — root cause or rationale
3. **What to do (or avoid) next time** — actionable guidance

---

## Required Fields

Every memory entry needs three fields:

| Field | Type | Description |
|---|---|---|
| `content` | `str` | Detailed description (min 30 chars). Specific, actionable, self-contained. |
| `type` | `str` | One of: `gotcha`, `failure_pattern`, `decision`, `convention`, `pattern`, `discovery`, `preference` |
| `tags` | `list[str]` | File paths touched, domain area, related tools |

### Tag Conventions

Tags enable future retrieval. Include all relevant dimensions:

```python
tags=[
    "file:bootstrap.py",       # file path touched (always prefix with file:)
    "file:dispatcher.py",       # multiple files OK
    "dispatcher",               # domain/module area
    "session-state",            # concept area
    "transform-pipeline",       # feature area
]
```

**Tag rules:**
- File paths use `file:` prefix — matches auto-tag enrichment format
- Domain tags are lowercase, hyphenated
- Include 3–7 tags per entry (too few = unfindable, too many = noise)

---

## When NOT to Save

| Situation | Why skip |
|---|---|
| Routine operation completed | "Read the table" — no insight, no future value |
| Information already in skills/instructions | Duplicate of documented knowledge — reference the skill instead |
| Duplicate of existing memory | Compare with bounded task selections and rely on save-time duplicate checks |
| Test passed without insight | "All tests green" adds nothing — only save if the test revealed something |
| Obvious fix | "Added missing import" — unless the import was non-obvious |
| Temporary workaround | Save only if the workaround will persist; otherwise it's noise |

---

## Session-End Capture and Assessment

When genuine reusable content exists, capture each bounded evidence-backed Observation and
assess the returned IDs. Otherwise assess with `nothing_reusable_learned`; do not
manufacture an Observation.

```python
observations = [
    anchor("learning", "capture", observation_type="reusable_practice", ...),
    anchor("learning", "capture", observation_type="near_miss", ...),
]
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[item["item"]["item_id"] for item in observations])
```

### Rules for canonical learning

- Capture requires a bounded supported Observation; assessment accepts IDs, not content
- Keep evidence, project/trust scope, and provenance explicit
- Gate creates a closure obligation when `files_changed > 0`; close it truthfully with
  real Observation IDs or `nothing_reusable_learned`

---

## Mid-Session Saves

Capture mid-session only when a supported reusable observation would otherwise be lost.
Elapsed time, novelty, or successful execution never creates a save obligation.

```python
# Only when current evidence supports a reusable observation
observation = anchor(
    "learning", "capture",
    observation_type="friction",
    summary="A verified reusable observation.",
    provenance={"source_action": "test", "source_version": "exact-head"},
    evidence=[{"reference_type": "test", "reference": "targeted check passed"}],
)
```

### When to Save Mid-Session vs Wait for Batch

| Save immediately | Wait for session end |
|---|---|
| Gotcha that cost >5 min to figure out | Minor convention observations |
| Error root cause after debugging | Decisions still being evaluated |
| Discovery that changes your approach | Patterns you used but aren't sure are reusable |
| Evidence-backed observation needed for closure | Low-stakes preferences |

---

## Session Notes Pattern

Maintain a running `_SESSION_NOTES` list during work. This prevents the "forgot what I discovered 20 minutes ago" problem.

```python
_SESSION_NOTES = []

# After each significant action, append a note
_SESSION_NOTES.append({
    "timestamp": "14:05",
    "action": "debugged KeyError in dispatcher",
    "finding": "dispatcher routes 'save' action to memory_context but the function "
               "signature changed — now requires entry_type as keyword-only arg",
    "save_worthy": True,
})

_SESSION_NOTES.append({
    "timestamp": "14:12",
    "action": "added entry_type param to save call",
    "finding": "straightforward fix, no insight",
    "save_worthy": False,
})

_SESSION_NOTES.append({
    "timestamp": "14:20",
    "action": "ran preflight on dispatcher.py",
    "finding": "preflight caught unused import (typing.Optional) — ruff I001. "
               "Also: function had 52 lines, extracted _resolve_entry_type() helper",
    "save_worthy": True,  # the extraction pattern is reusable
})
```

### At Session End — Convert Notes to Saves

```python
# Filter to save-worthy notes
worthy = [n for n in _SESSION_NOTES if n["save_worthy"]]

# Save only deduplicated genuine content; an empty set closes as no reusable learning.
if worthy:
    observations = [anchor("learning", "capture", ...) for note in worthy]
    anchor("learning", "assess", outcome="observations_recorded",
       observation_ids=[item["item"]["item_id"] for item in observations])
else:
    anchor("learning", "assess", outcome="nothing_reusable_learned")
```

---

## Deduplication

Before saving, compare the insight with the accepted task's bounded `memory_context`.

```python
# Use task_result["memory_context"]["selections"], not a full-store query.
# Save only if the insight is genuinely new.
# If a similar selection exists, consider whether yours adds new information.
# If it does → save (the system handles near-duplicates)
# If it doesn't → skip
```

### Signs You're About to Create a Duplicate

- The content starts with the same error message as an existing entry
- You're restating a convention that's already documented
- The "insight" is something you read from an existing memory entry earlier in the session

---

## Thread-to-Thread Handoff Memory

When ending a thread to hand off work to another agent or future session, preserve all
material resumable state that took effort to compile. The next thread should not have to
re-derive non-obvious state.

### Use `anchor("snapshot", mode="handoff")` with Detailed State

```python
anchor("snapshot", mode="handoff",
    summary="Implementing transform rollback with Spark checkpoints",
    state="in_progress",
    decisions=[
        "Using cache() not localCheckpoint() — need full lineage for debugging",
        "Checkpoint dict keyed by step_order (int), not action name — names can repeat",
        "unpersist() iterates in reverse order to avoid dependency issues",
    ],
    artifacts=[
        {"path": "src/tables.py", "role": "source", "note": "rollback implementation"},
        {"path": "src/dispatcher.py", "role": "source", "note": "dispatch integration"},
    ],
    evidence_chain=[
        {"tool": "pytest", "summary": "5/7 passing; eviction tests remain", "status": "partial"},
        {"tool": "trace", "summary": "Py4J unpersist failure resolved with is_cached guard", "status": "resolved"},
    ],
    next_steps=[
        "Add max_checkpoints FIFO eviction and its rollback regression",
    ],
)
```

### What to Include in Handoff

| Include | Why |
|---|---|
| File paths + line numbers | Next thread can jump directly to the code |
| Function signatures (full) | Prevents wrong-signature calls |
| Error messages (exact text) | Enables instant `anchor("trace")` lookup |
| Parameter values that worked | Eliminates trial-and-error |
| Decisions made + alternatives rejected | Prevents re-debating |
| What's left to do (specific) | Clear starting point |
| Test status | Knows what's verified vs not |

### What NOT to Include in Handoff

- Routine actions ("I read the file") — only include what was learned
- Full file contents — reference by path instead
- Steps that will be obvious from the code — focus on non-obvious state

---

## Integration with odibi-anchor

```python
# Mid-session: capture only a supported reusable observation when needed.

# Check task_result["memory_context"]["selections"] for duplicates before saving.
# Deep diagnostics require an explicitly authorized memory-governance task.

# Session end: save genuine content or assess nothing_reusable_learned

# Thread handoff: preserve implementation state
anchor("snapshot", mode="handoff", summary="task description", state="in_progress", decisions=[...])

# Session snapshot: capture current state for resume
anchor("snapshot", decisions=[...], next_steps=[...])

# Store-wide memory diagnostics belong only to an explicitly authorized
# memory-governance task; routine work does not call memory_stats.
```

### Decision Flow

```
Action completed
  → Was there an insight? (not just "it worked")
    → NO: don't save
    → YES: Is it evidence-backed and reusable beyond this execution?
      → YES: capture a bounded Observation
      → NO: add to _SESSION_NOTES for truthful closure assessment
  → Ending session?
    → Review _SESSION_NOTES for save_worthy items
    → Check bounded task memory for duplicates
    → Capture supported observations, or assess nothing_reusable_learned
  → Handing off?
    → anchor("snapshot", mode="handoff", ...) with all material compiled details
```

## Memory Curation: task disposition/evaluation and `anchor("reject")`

After `anchor("memory")` loads entries, you may find stale or wrong ones. Curate them:

```python
# Step 1: Load memories and review
result = anchor("memory", query="null handling in dedup")
# → Shows entries with IDs

# Step 2: Keep correct candidates advisory. Use/evaluation counters do not promote;
# authority requires a typed verifier or separate governed owner requests.
# If a task-selected memory materially affects work, record its applied
# disposition/application and evaluate it with retained evidence.
application = anchor("memory", "apply", selection_id="sel_abc123", action="...")
anchor("memory", "evaluate", application_id=application["application_id"],
   outcome="helpful", evidence={"check": "..."})

# Step 3: Reject entries that are stale or wrong
result = anchor("reject", "mem_def456")
# → Marks as rejected, excluded from future retrieval
```

**When to curate:**

| Situation | Action |
|---|---|
| Task-selected memory materially affected work | Record applied disposition/application and evidence-backed evaluation; it remains a candidate |
| Convention changed since entry was saved | `anchor("reject", "entry_id")` + save new |
| Entry is duplicate of a better one | `anchor("reject", "entry_id")` |
| Entry was a guess that turned out wrong | `anchor("reject", "entry_id")` |
| Obsolete actionable guidance | Run `anchor("memory_hygiene")`, review deterministic IDs, then apply those exact IDs for quarantine |

**NEVER leave stale entries unrejected** — they pollute future `anchor("memory")` loads and cause the agent to repeat old mistakes.

`anchor("confirm", ...)` is blocked legacy compatibility and returns `confirmation_blocked`.
It is not a promotion path. Mechanically provable claims use the typed verifier path;
preferences, conventions, policy, and procedural authority use separate governed owner
requests: `request_owner_activation` for candidate→active, then
`request_owner_confirmation` for active→confirmed. Retrieval, application, evaluation,
recurrence, task success, and counters never promote memory. Complete Slack configuration uses
remote authenticated replies; otherwise an interactive Windows session opens a local Yes/No
owner-presence dialog. A single-user Databricks session first prepares an exact challenge without
authority mutation, then requires the owner to send that phrase in a new Genie message before a
second call supplies `in_session_approval`. Its immutable receipt labels an in-session assertion,
not authenticated identity; workspace or Genie execution approval alone is insufficient.

### Compatibility-only historical recovery

Legacy `anchor("learn", session_events=[...])` remains callable only for historical recovery
that technically requires its old payload. It is not the normal closure route. A gate or
checkpoint opens an obligation; explicit captures record Observations and `learning assess`
closes it. A truthful no-learning assessment creates no memory.
