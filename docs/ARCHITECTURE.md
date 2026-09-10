# Architecture

## Package Layout

```
src/odibi_anchor/
├── __init__.py          Top-level exports + lazy loading
├── _utils/
│   ├── contract.py      Output contract builder + validation
│   ├── render_utils.py  Markdown rendering primitives
│   ├── engine_utils.py  DataFrame engine detection (pandas vs Spark)
│   └── ast_utils.py     AST inspection helpers (stdlib ast)
├── planning/
│   ├── task_execution_context.py  Task planning + readiness scoring
│   ├── quick_context.py           Lightweight planning brief
│   └── handoff_context.py         Cross-thread handoff state
├── validation/
│   ├── quality_gate_context.py    Pre-write quality checks
│   ├── validation_summary_context.py  Rule-based validation
│   └── duplicate_key_context.py   Key integrity analysis
├── tables/
│   ├── diff_ops.py               Row-level table comparison
│   ├── compare_ops.py            Cross-check + delete detection
│   ├── schema_diff_context.py    Schema comparison
│   └── table_contract_summary.py Table grain + contract
├── profiling/
│   │   (statistical profiling / first-look orientation now live in
│   │    tools/table_profiler_tool — anchor("profile_table"))
│   └── dogfood_regression_context.py  Output regression tracking
├── codebase/
│   ├── codebase_map_context.py      Codebase orientation + structure
│   ├── change_impact_context.py     Downstream impact analysis
│   ├── session_snapshot_context.py  Session state persistence
│   ├── consistency_check_context.py Convention enforcement
│   ├── convention_preflight_context.py  Pre-build convention check
│   ├── test_focus_context.py        Targeted test identification
│   ├── workflow_gate_context.py     Obligation debt tracker
│   ├── framework_lookup_context.py  Framework function finder
│   ├── memory_context.py           Project memory query + write
│   ├── semantic_edit_context.py    Intent-based code editing
│   ├── preflight_context.py        Lint + type check runner
│   ├── import_resolve_context.py   Import path resolver
│   ├── safe_change_context.py      Full edit→verify pipeline
│   ├── learn_context.py            Compatibility-only historical recovery when old persisted debt technically requires it
│   └── known_bad_change_context.py Pre-edit guardrail
└── debugging/
    ├── error_trace_context.py      Structured traceback analysis
    └── failure_pattern_context.py  Known error pattern matching
```

## Tool Count by Package

| Package | Tools | Count |
|---|---|---|
| planning | task_execution_context, quick_context, handoff_context | 3 |
| validation | quality_gate_context, validation_summary_context, duplicate_key_context | 3 |
| tables | diff_tables_by_key, schema_diff_context, table_contract_summary | 3 |
| profiling | dogfood_regression_context (+ profile_table in tools/table_profiler_tool) | 1 |
| codebase | codebase_map_context, change_impact_context, session_snapshot_context, consistency_check_context, convention_preflight_context, test_focus_context, workflow_gate_context, framework_lookup_context, memory_context, semantic_edit_context, preflight_context, import_resolve_context, safe_change_context, learn_context, known_bad_change_context | 15 |
| debugging | error_trace_context, failure_pattern_context | 2 |
| **Total** | | **27** (+ append_memory, confirm_memory, reject_memory helpers) |

## Dependency Graph

```
stdlib only:
  planning/*
  codebase/codebase_map_context.py
  codebase/change_impact_context.py
  codebase/session_snapshot_context.py
  codebase/consistency_check_context.py
  codebase/convention_preflight_context.py
  codebase/test_focus_context.py
  codebase/workflow_gate_context.py
  codebase/framework_lookup_context.py
  codebase/memory_context.py
  codebase/learn_context.py
  codebase/known_bad_change_context.py
  codebase/import_resolve_context.py
  debugging/*

optional libcst:
  codebase/semantic_edit_context.py  (falls back to regex if missing)
  codebase/safe_change_context.py   (delegates to semantic_edit_context)

subprocess tools (pyright, ruff):
  codebase/preflight_context.py  (degrades gracefully if not installed)

pandas (+ numpy):
  _utils/engine_utils.py
  validation/*
  tables/*
  profiling/*

optional pyspark:
  All pandas features also support Spark DataFrames
  (engine auto-detected or specified via engine= parameter)
```

## Memory & Telemetry

Project memory is the **local runtime knowledge store**: a SQLite database
(`.agent_memory.db`) at the Odibi Anchor root. The live database is git-ignored
runtime state; it is not committed or synchronized across agents or machines by Git.
Its path is resolved per environment from the active profile
(`_boot._ENV["memory_db"]` → `<anchor_root>/.agent_memory.db`), with a `ANCHOR_MEMORY_DB`
override used to isolate test runs. A full-text-search index over `content`
backs `memory_context()` queries, with a SQL fallback.

The memory domain implements a seven-stage operating cycle: **Observe, Encode, Retrieve,
Apply, Evaluate, Consolidate, Govern**. Its CoALA categories and authority boundaries are:

| Category | Owner | Boundary |
|---|---|---|
| Working | Process/task-scoped `SessionState` | Transient execution context |
| Episodic | Structured-learning observations, evidence, assessments | Recorded experience, not a semantic claim |
| Semantic | `memories` plus lifecycle selections/applications/evaluations | Advisory reusable knowledge |
| Procedural | Distributed immutable skills and references | Changes require separately authorized source work |

Problems, Specs, decisions, and work items remain authoritative. Retrieval cannot grant
authority or replace verification. Task acceptance derives a bounded semantic query from the
accepted description, goal, profile, linked artifacts, project, and repository scope. Stable
selection records preserve why an entry was exposed without treating exposure as application.
Applications and their evidence-backed evaluations are separate lifecycle records.

Human-attested structured-learning triage is the consolidation boundary. Publishing an
active lesson/watch creates one idempotent semantic candidate with projection lineage; no
observation, recurrence, retrieval, or successful run promotes itself.

The forensic journal is append-only, redacted, keyed by `task_window_id`, and protected by a
per-task SHA-256 hash chain. Replay only inspects events, verifies integrity, or builds a
deterministic context package with explicit unavailable evidence. It does not execute writes
or effects and does not retain or reconstruct chain-of-thought, secrets, or raw private data.

Storage governance reports profile, trust domain, canonical-path safety, repository overlap,
schema, and backup state without moving data. Migration support produces a non-destructive
plan; an actual migration or cross-domain transfer requires separate owner approval.

Each `memories` row carries:

```json
{
  "id": "m0001",
  "type": "gotcha|failure_pattern|decision|pattern",
  "content": "Human-readable description",
  "related_files": ["glob/patterns/**/*.py"],
  "tags": ["keyword", "tags"],
  "source": "manual|learn_context|auto-learned",
  "added": "2026-05-12",
  "last_used": "2026-05-12",
  "use_count": 3,
  "confidence": 0.9,
  "status": "confirmed|candidate|rejected",
  "confirmation_count": 3,
  "false_positive_count": 0,
  "sessions_seen": 1,
  "evidence": {}
}
```

Operations: `memory_context()` (query), `append_memory()` (candidate-only write),
`reject_memory()` (exclude wrong entries), and `learn_context()` (compatibility-only historical
recovery when old persisted debt technically requires it). Normal learning uses
`anchor("learning", "capture|assess", ...)`. `confirm_memory()` is blocked legacy compatibility
and returns `confirmation_blocked`. Promotion authority comes only from supported typed
verifiers or separate governed owner activation and confirmation requests; retrieval,
application, evaluation, task success, and counters never promote. Owner requests prefer a
fully configured authenticated Slack transport, otherwise use an explicit local Windows
owner-presence dialog or a two-step single-user Databricks in-session assertion. Databricks
preparation grants nothing; the owner must send the exact challenge in a new Genie message before
the second call. That receipt is labeled lower assurance and does not claim workspace
authentication proves owner identity or transition approval. The immutable receipt identifies
which assurance boundary was used.
Session telemetry
(compliance, timings) is recorded in a separate
`session_audits` table in the same DB. Markdown export/import
(`export_markdown()` / `import_from_markdown()`) provides a human-readable view
for review. Transient per-session files (`.anchor_session_state.json`,
`.session_snapshot.json`, `.patch_log.jsonl`, …) are git-ignored.

## Design Decisions

1. **No base classes** — each generator is a standalone function
2. **No global state** — all inputs explicit, all outputs serializable
3. **No LLM calls** — pure computation, deterministic outputs
4. **No framework deps** — only stdlib, pandas, numpy, (optional pyspark)
5. **Dual output** — dict (for machines) + markdown (for humans)
6. **Engine detection** — single 70-line utility, not a framework
7. **Optional deps degrade gracefully** — libcst, pyright, ruff are optional; tools fall back to simpler methods when absent

## Output Contract

All context generators return dicts with these 8 standard keys:

- `kind` (str): Generator name (e.g., "memory_context")
- `subject` (str): What was analyzed
- `summary` (str): Human-readable one-liner
- `metrics` (dict): Numeric counts and measures
- `findings` (list[str]): Observations and results
- `risks` (list[str]): Issues requiring attention
- `samples` (dict): Example data for inspection
- `suggested_next_actions` (list[str]): MUST/SHOULD-prefixed next steps

Additional keys vary per generator (e.g., `matched_memories`, `entries`, `diff_preview`).
All outputs are JSON-serializable.

## Testing Strategy

- Pure pandas tests (no Spark cluster needed for CI)
- Spark tests marked with `@pytest.mark.skip` when no cluster available
- Codebase tools tested with `tmp_path` fixtures (filesystem isolation)
- Edge cases: empty DF, single row, all nulls, type mismatches, missing files
- Output contract validation: JSON-serializable, 8 expected keys present
- Tests organized by package: `tests/{package}/test_{module}.py`

## See Also

- [Tool Picker](guides/tool_picker.md) — "which tool do I use?" quick reference
- [Quickstart](guides/quickstart.md) — installation and first usage
