# Multi-session planning

> Preserved bounded-phase and continuation technique.

Structured planning before execution. Think → Plan → Critique → Execute.
Load this when a task is non-trivial: multi-step, multi-table, or involves design decisions.

## When to Use This Skill

| Task type | Planning level |
|---|---|
| Show schema, sample data, single query | SKIP — just do it |
| Single transform, known pattern | LIGHTWEIGHT — concise framing and short plan |
| Multi-step pipeline, some unknowns | FULL — complete loop below |
| Multi-layer, new domain, architecture | FULL + get user approval before executing |

## The Loop

```
THINK → PLAN → CRITIQUE → REVISE → EXECUTE
  ↑                          |
  └──────────────────────────┘  (loop until critique passes)
```

## Step 1: THINK — Understand Before Acting

Answer these out loud before planning:

```
🧠 THINK
├── What is the user actually asking? (one sentence)
├── What do I know?
│   ├── Tables/data involved
│   ├── Relevant framework functions (from anchor("lookup"))
│   └── Current repository and authority context
├── What do I NOT know?
│   ├── Assumptions I'm making
│   ├── Things I need to verify (schema, cardinality, grain)
│   └── Ambiguities to clarify with user
└── What is the simplest version of "done"?
```

Prioritize the unknowns most likely to change the approach. For complex work, split them
into material, independently testable branches and identify confirming and disconfirming evidence.

**Rules:**
- If you can't restate the goal in one sentence → ask the user
- Verify assumptions that could materially change the approach
- "What do I NOT know" is the most important question

## Step 2: PLAN — Design the Approach

```
📋 PLAN
├── Approach (concise)
├── Steps (numbered, each is a concrete verifiable action)
│   ├── 1. ...
│   ├── 2. ...
│   └── N. ...
├── Framework functions I'll use
├── What I'll verify after each step
└── Complexity: SIMPLE | MODERATE | COMPLEX
```

### For Pipelines — Source-to-Target Map

| Source | Target | Layer | Strategy | Keys |
|---|---|---|---|---|
| SharePoint/file.xlsx | bronze.raw_table | Bronze | append | _source_file |
| bronze.raw_table | silver.clean_table | Silver | upsert | id, snapshot_month |
| silver.clean_table | gold.dim_table | Gold | overwrite | — |

### Key Decisions (document BEFORE building)

| Decision | Rationale | Alternatives Considered |
|---|---|---|
| Grain: one row per project per snapshot | Source has monthly files, need history | Daily grain (rejected: no daily signal) |
| Dedup on project_id + snapshot_month | Cardinality check confirmed uniqueness | project_id alone (rejected: collapses history) |

## Step 3: CRITIQUE — Poke Holes

```
🔍 CRITIQUE
├── Am I solving the right problem? (re-read user's request)
├── Am I over-engineering?
│   ├── Could this be fewer steps?
│   ├── Am I adding abstraction that isn't needed?
│   └── Would a simpler approach work?
├── Am I under-engineering?
│   ├── Nulls, duplicates, dirty data handled?
│   ├── Idempotent on re-run?
│   └── Missing framework functions I should use?
├── What could go wrong?
│   ├── Data risk: unexpected nulls, schema drift
│   ├── Logic risk: wrong keys, wrong join type
│   └── Silent failure: code runs, produces wrong results
└── If a senior engineer reviewed this, what would they question?
```

**Rules:**
- Record material findings honestly; a sound plan may have none
- "What would a senior engineer question?" is the strongest prompt
- If critique reveals a fundamental issue → go back to THINK

## Step 4: REVISE — Update Based on Critique

```
✏️ REVISE
├── Changes from original plan:
│   ├── [change]: because [critique finding]
│   └── ...
├── Risks I'm accepting (and why):
│   └── [risk]: acceptable because [reason]
├── Residual uncertainty and stop conditions
└── Final plan (updated steps)
```

## Step 5: EXECUTE — Build Step by Step

- **After task acceptance and before writing code:** review only the accepted task's bounded
  `memory_context`; acknowledge selected IDs and metadata or none without forcing use
- **Before writing code:** apply the repository's governing code standards
- Follow the revised plan
- After each step: verify it worked (row count, sample, key check)
- If something unexpected → mini-critique before continuing
- Build and verify one layer at a time (bronze → silver → gold)
- **After final step:** `anchor("gate")` to confirm all obligations met — no args needed, derives evidence from session state
- **At learning closure:** capture supported reusable observations, or assess `nothing_reusable_learned`

## Task Tracking (Multi-Session Work)

For work that spans sessions, maintain a `_tasks/backlog.md`:

```markdown
# Project Task Backlog

> Last updated: YYYY-MM-DD

## Active
| ID | Task | Status | Depends On |
|----|------|--------|------------|
| 001 | Build bronze ingestion | 🔵 3/5 | — |
| 002 | Build silver dedup | 🔴 Blocked | 001 |

## Ready
| ID | Task | Depends On |
|----|------|------------|
| 003 | Build gold view | 002 |
```

### Status Values
- ⏳ Ready — defined, not started
- 🔵 In Progress (X/Y steps)
- ✅ Done
- 🔴 Blocked — waiting on dependency
- 🟡 Parked — deprioritized with reason

### Session Handoff

When stopping mid-task, update "What the next session needs to know":
1. Where you stopped (which step)
2. What's working so far
3. What surprised you (unexpected schema, dirty data)
4. What the next step should do specifically
5. Key config values/parameters needed

## Presenting Plans to User

Present the plan when useful for review. Wait only when it contains a genuine architecture,
scope, destructive-effect, or materially ambiguous decision:

```
Here's my plan for [task]:

**Goal:** [one sentence]

**Steps:**
1. ...
2. ...
3. ...

**Key decisions:**
- [decision]: because [reason]

**Risks:**
- [risk]: mitigated by [approach]

Ready to proceed, or want to adjust anything?
```

## Anti-Patterns

| Don't | Do Instead |
|---|---|
| Skip straight to code | Run think → plan → critique first |
| Plan hides material uncertainty | Split warranted branches into independently verifiable actions |
| Invent a critique finding | Record material findings or state that none were found |
| Build all layers at once | One layer at a time, verify between |
| Assume column names/types | Explore/profile the data first |
| Over-abstract on first pass | Start concrete, extract patterns when reuse is proven |

## Integration with odibi-anchor

- `anchor("task", "description", goal="...", mode="implementation")` — structured plan scaled to the task
- accepted task `memory_context` — bounded advisory context to review before source edits
- `anchor("snapshot", mode="handoff", summary="task", state="in_progress", decisions=[...])` — persist state for next session
