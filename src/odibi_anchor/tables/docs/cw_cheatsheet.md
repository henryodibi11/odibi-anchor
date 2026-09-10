# anchor() Cheat Sheet — Action → Source File

Every `anchor("action", ...)` call, where it lives in the codebase, and what it does.

---

## Session & Workflow Control

| Action | Source | What it does |
|---|---|---|
| `status` | `_dispatcher/_session_tools.py` | Orientation hub — compliance score, next required step, write guard |
| `memory` | `codebase/` | Load prior learnings relevant to current project |
| `audit_history` | `_dispatcher/_session_tools.py` | Recent compliance audit scores and recurring gaps |
| `new_session` | `_dispatcher/_session_tools.py` | Create session notebook, enforce learn-debt check |
| `task` | `planning/` | Full planning — goal, mode, known_facts, constraints, acceptance_criteria |
| `review` | `codebase/review_context.py` | Review session diff and obligations before gate |
| `gate` | `codebase/` | Compliance gate — verifies preflight, tests, touched, learn all ran |
| `learn` | `codebase/` | Persist session findings to memory bank |
| `skill_loaded` | `bootstrap.py` (inline) | Register that a skill was loaded this session |
| `touched` | `_dispatcher/_session.py` | Record a file edit in session state (required after every edit) |
| `checkpoint` | `_dispatcher/_checkpoint.py` | Save intermediate session state for rollback |
| `snapshot` | `codebase/` | Full session snapshot |
| `spec` | `_dispatcher/_spec.py` | Spec-driven workflow — persist, validate, execute, review |
| `handoff` | `planning/` | Hand off context to next agent/session |

---

## Data Profiling — Investigation Chain

| Action | Source | What it does |
|---|---|---|
| `profile_table` | `_dispatcher/_tool_wrappers.py` → `tools/table_profiler_tool/lib/profiler.py` | Full table scan — grain, freshness, all columns, format issues, outliers |
| `microscope` | `_dispatcher/_tool_wrappers.py` → `tools/table_profiler_tool/lib/microscope.py` | Deep dive into one column — distribution, percentiles, skewness, outliers |
| `case_file` | `_dispatcher/_tool_wrappers.py` → `tools/table_profiler_tool/lib/case_file.py` | Row-level investigation — filter by nulls, outliers, top/bottom N, where expr |
| `explore` | `profiling/` | Quick DataFrame exploration — shape, types, nulls |
| `profile` | `profiling/` | Dataset profile context (broader than table_profiler) |
| `dogfood` | `profiling/` | Dogfood regression context — tool output validation |
| `chain` | `_dispatcher/_tool_wrappers.py` | Multi-step tool chain runner |

> **Investigation chain:** `profile_table` → `microscope` → `case_file`

---

## Data Comparison

| Action | Source | What it does |
|---|---|---|
| `diff` | `tables/diff_ops.py` | Row-level diff keyed on ID — added, removed, changed, unchanged |
| `coerce_check` | `tables/coercion_classifier.py` | Classify why values changed — whitespace, case, unicode, truncation |
| `schema_diff` | `tables/schema_diff_context.py` | Column-level schema comparison between two DataFrames |
| `contract` | `tables/table_contract_summary.py` | Table contract summary — expected shape, types, grain |

> **Comparison chain:** `diff` → `coerce_check` (on columns with `value_changed > 0`)

---

## Data Quality & Transform

| Action | Source | What it does |
|---|---|---|
| `quality` | `validation/` | Quality gate — summarizes issues before deciding to transform |
| `validate` | `validation/` | Run validation rules against a DataFrame |
| `duplicate` | `validation/` | Duplicate key detection |
| `transform` | `tables/transform_plan_context.py` | Generate transform plan from profile/quality output |
| `apply_transform` | `tables/apply_transform_context.py` | Execute transform plan — no eval(), handler dispatch, per-step isolation |
| `rollback` | `tables/__init__.py` | Revert to a named checkpoint from an apply_transform result |
| `unpersist` | `tables/__init__.py` | Free cached checkpoints from an apply_transform result |

> **Quality chain:** `quality` → `validate` → `transform` → `apply_transform` → `rollback` (if needed)

---

## Codebase & Dev Tools

| Action | Source | What it does |
|---|---|---|
| `safe` | `codebase/` | Safe file edit with write guard and session tracking |
| `semantic` | `codebase/` | Semantic (intent-based) file edit |
| `known_bad` | `codebase/` | Record pre-edit baseline — required before any .py edit |
| `preflight` | `codebase/` | Lint + import check on changed files |
| `test` | `_dispatcher/_session_tools.py` | Run tests scoped to changed files |
| `map` | `codebase/` | Codebase map — file structure and dependency overview |
| `impact` | `codebase/` | Change impact analysis — what else could this edit affect |
| `consistency` | `codebase/` | Consistency check — naming, patterns, conventions |
| `convention` | `codebase/` | Convention preflight — coding standards check |
| `import_resolve` | `codebase/` | Resolve import errors and missing dependencies |
| `lookup` | `codebase/` | Search framework for existing functions (162+) |
| `error` | `debugging/` | Failure pattern analysis — classify and explain an error |
| `trace` | `debugging/` | Error trace context — stack trace walkthrough |

---

## Memory & Knowledge

| Action | Source | What it does |
|---|---|---|
| `memory` | `codebase/` | Load relevant prior session learnings |
| `save` | `codebase/` | Append a finding to memory bank |
| `memory promotion` | `codebase/` | Typed-verifier promotion, or governed owner-presence activation then confirmation for conventions/preferences |
| `confirm` | `codebase/` | Blocked legacy compatibility: returns `confirmation_blocked` and does not promote |
| `reject` | `codebase/` | Reject a pending memory entry |
| `learn` | `codebase/` | Compatibility-only historical recovery when old persisted debt technically requires it; normal route is `learning capture/assess` |

---

## Workflows (Multi-Step)

Pre-built sequences that chain multiple primitive tools together.

| Action | Source | What it does |
|---|---|---|
| `onboard` | `_dispatcher/_workflows.py` | Full onboarding flow for a new data source |
| `reconcile` | `_dispatcher/_workflows.py` | Reconciliation — diff + coerce_check + report |
| `investigate` | `_dispatcher/_workflows.py` | Guided investigation — profile + microscope + case_file |
| `debug` | `_dispatcher/_workflows.py` | Debug workflow — error → trace → fix |
| `trace_row` | `_dispatcher/_workflows.py` | Trace a specific row through a pipeline |
| `evolve` | `_dispatcher/_workflows.py` | Schema evolution workflow |

---

## Source Tree (quick reference)

```
odibi_anchor/
├── src/odibi_anchor/
│   ├── bootstrap.py                   ← dispatcher entrypoint, init()
│   ├── planning/                      ← task, handoff
│   ├── codebase/                      ← safe, semantic, known_bad, preflight, gate,
│   │                                    learn, memory, save, map, impact, lookup,
│   │                                    review, snapshot
│   ├── tables/                        ← diff, coerce_check, schema_diff, contract,
│   │   ├── diff_ops.py                  transform, apply_transform, rollback, unpersist
│   │   ├── coercion_classifier.py
│   │   ├── schema_diff_context.py
│   │   ├── transform_plan_context.py
│   │   ├── apply_transform_context.py
│   │   └── table_contract_summary.py
│   ├── validation/                    ← quality, validate, duplicate
│   ├── profiling/                     ← explore, profile, dogfood
│   ├── debugging/                     ← error, trace
│   └── _dispatcher/
│       ├── _tool_wrappers.py          ← profile_table, microscope, case_file, chain
│       ├── _workflows.py              ← onboard, reconcile, investigate, debug,
│       │                                trace_row, evolve
│       ├── _session.py                ← touched
│       ├── _session_tools.py          ← status, audit_history, new_session, test
│       ├── _checkpoint.py             ← checkpoint
│       └── _spec.py                   ← spec
└── tools/
    └── table_profiler_tool/lib/       ← actual profiler implementations
        ├── profiler.py                  (profile_table delegates here)
        ├── microscope.py                (microscope delegates here)
        ├── case_file.py                 (case_file delegates here)
        └── renderer.py                  (all markdown rendering)
```
