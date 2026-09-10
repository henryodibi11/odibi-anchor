# Table Profiler Tool — Architecture

## System Overview

The table profiler tool transforms any DataFrame (pandas or Spark) into a comprehensive
`TableProfile` dataclass through a deterministic 13-step pipeline. The orchestrator
(`profiler.py`) coordinates 15 specialized sub-modules, each responsible for one
aspect of profiling.

```
Input DataFrame
      │
      ▼
┌─────────────────┐
│ Engine Detection │  (_sampling.py → "pandas" or "spark")
│ + Shape          │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│ Early Materialization   │  (Spark → pandas if rows ≤ 500K & cols ≤ 100)
│ (performance shortcut)  │  rows × cols > threshold → stays Spark
└────────┬────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────────┐
│                    13-Step Core Pipeline                        │
│                                                                │
│  Step 1:  stats_engine        → ColumnProfile per column       │
│  Step 2:  semantic_typer      → SemanticType inference          │
│  Step 3:  role_classifier     → ColumnRole inference            │
│  Step 4:  grain_detector      → GrainAnalysis (standard+)       │
│  Step 5:  freshness           → FreshnessAnalysis (standard+)   │
│  Step 6:  table_classifier    → TableClassification (standard+) │
│  Step 7:  format_checker      → FormatIssue list (standard+)    │
│  Step 8:  cleanliness         → FormatIssue list (standard+)    │
│  Step 9:  duplicate_forensics → DuplicateForensics (standard+)  │
│  Step 10: string_length       → FormatIssue list (standard+)    │
│  Step 11: outlier detection   → OutlierProfile list (standard+) │
│  Step 12: cross_column        → co-null patterns (standard+)    │
│  Step 13: value_stability     → drift analysis (standard+)      │
│                                                                │
└────────────────────────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────────┐
│                  Auxiliary Steps (conditional)                  │
│                                                                │
│  join_profiler.py   → JoinProfile list                         │
│                       Only runs when reference_tables= provided │
│                                                                │
│  findings_extra.py  → Additional findings                       │
│                       Runs post-assembly to cross-reference     │
│                       results from multiple steps               │
└────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────┐
│ Assembly + Quality Score │
│ → TableProfile dataclass │
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│ contract.py              │  serialize_profile() → Anchor standard dict
│ renderer.py              │  render_table_profile_md() → markdown
└──────────────────────────┘
```

## Three Zoom Levels

| Level | Function | Purpose | Output |
|-------|----------|---------|--------|
| Table | `profile_table(df, subject, level)` | Full table profiling pipeline | `TableProfile` dataclass |
| Column | `microscope(df, column, ...)` | Single-column deep-dive | Anchor standard dict |
| Row | `case_file(df, column=..., filter=...)` | Row-level investigation | Anchor standard dict |

Each zoom level is independently callable. `microscope` and `case_file` can accept
a pre-computed `TableProfile` via the `profile=` parameter to avoid redundant statistics.

## Module Dependency Graph

```
profiler.py (orchestrator)
├── _sampling.py          → detect_engine(), early materialization
├── stats_engine.py       → compute_column_stats() → list[ColumnProfile]
├── semantic_typer.py     → infer_semantic_type() → Inference[SemanticType]
├── role_classifier.py    → infer_column_role() → Inference[ColumnRole]
├── grain_detector.py     → detect_grain() → GrainAnalysis
├── freshness.py          → detect_freshness() → FreshnessAnalysis
├── table_classifier.py   → classify_table() → Inference[TableClassification]
├── format_checker.py     → detect_format_issues() → list[FormatIssue]
├── cleanliness.py        → audit_cleanliness() → list[FormatIssue]
├── duplicate_forensics.py→ analyze_duplicates() → DuplicateForensics
├── string_length.py      → detect_string_length_issues() → list[FormatIssue]
├── cross_column.py       → detect_cross_column_dependencies()
├── value_stability.py    → analyze_value_stability()
├── join_profiler.py      → profile_joins() → list[JoinProfile]  [conditional]
├── findings_extra.py     → detect_extra_findings()              [post-assembly]
└── extension_registry.py → plugin extensions

microscope.py (standalone)
├── _sampling.py
├── _common.py
└── models.py

case_file.py (standalone)
├── _sampling.py
├── _common.py
└── models.py

contract.py (serialization)
└── models.py

renderer.py (display)
└── models.py
```

## Key Data Models (models.py)

### Core Dataclasses

| Dataclass | Fields | Purpose |
|-----------|--------|---------| 
| `TableProfile` | 40+ fields | Complete profiling result |
| `ColumnProfile` | 50+ fields | Per-column statistics and metadata |
| `GrainAnalysis` | best_grain, is_unique, duplicate_rate, candidates_tested | Primary key detection |
| `FreshnessAnalysis` | freshness_column, latest_value, staleness, cadence | Data recency |
| `DuplicateForensics` | grain_columns, duplicate_count, concentration_column, verdict | Duplicate root cause |
| `JoinProfile` | source_column, target_table, overlap_pct, orphan_count, safe_join_type | Joinability |
| `OutlierProfile` | column, method, lower_bound, upper_bound, outlier_count, outlier_pct | Numeric anomalies |
| `FormatIssue` | issue_type, column, severity, affected_count, fix_suggestion | Quality problems |

### Inference Pattern

```python
@dataclass
class Inference:
    value: Any                          # The inferred classification
    confidence: float                   # 0.0–1.0
    evidence: list[str]                 # Signals supporting this inference
    counter_signals: list[str]          # Signals against it
    sample_size: int                    # How many rows were evaluated
    method: str                         # Algorithm used
    runner_ups: list[CompetingHypothesis]  # Alternative candidates
    blocker_reason: str                 # Why confidence may be capped

@dataclass
class CompetingHypothesis:
    value: Any
    confidence: float
    evidence: list[str]
    counter_signals: list[str]
    blocker_reason: str
    verification_hint: str              # How to disambiguate
```

Every classification decision (semantic type, column role, table classification, grain)
uses this `Inference` wrapper. This makes all profiler outputs explainable — you can
always inspect WHY a decision was made and what alternatives were considered.

### Enums

| Enum | Values | Used By |
|------|--------|---------| 
| `TableClassification` | fact, dimension, snapshot, event_log, scd2, lookup, staging, aggregate, bridge, unknown | table_classifier |
| `ColumnRole` | primary_key, natural_key, surrogate_key, foreign_key, measure, dimension, flag, timestamp, partition, derived, metadata, freetext, identifier, unknown | role_classifier |
| `SemanticType` | email, phone, url, uuid, ip_address, zip_code, state_code, country_code, currency_amount, percentage, date_string, boolean_string, json_string, delimited_list, file_path, enum, code, freetext, numeric_string, unknown | semantic_typer |
| `ProfilingLevel` | quick, standard, deep | profiler |
| `IssueSeverity` | error, warning, info | format_checker, cleanliness |

## Design Decisions

### Why 15 Sub-Modules?

1. **Separation of concerns** — each module tests independently without mocking the others
2. **Graceful degradation** — each step is wrapped in try/except; failures add to `degraded_features` without crashing the full profile
3. **Level gating** — steps 4–13 only run at standard+ level, keeping "quick" profiles under 100ms
4. **Composability** — `microscope` and `case_file` reuse `_sampling.py` and `_common.py` without importing the orchestrator

### Why Early Materialization?

For tables ≤ 500K rows × 100 columns, converting Spark to pandas up-front eliminates
all per-column Spark round-trips. The threshold keeps memory safe (~200–400MB) while
offering 10–50x speedup on typical Databricks clusters.

Tables exceeding this threshold (large Spark DataFrames) stay on the Spark path throughout.
If you're profiling very large tables and seeing slow performance, consider filtering to
a representative sample before calling `profile_table`.

### Why the Inference/CompetingHypothesis Pattern?

Many profiler decisions are inherently probabilistic ("Is this column a primary key
or a foreign key?"). The Inference pattern:
- Makes confidence explicit and machine-readable
- Preserves runner-ups so downstream tools can validate or override
- Provides `verification_hint` for human review when confidence is low
- Enables diffing between profile runs (confidence changes signal drift)

### Why extension_registry?

Plugin support for custom sub-modules. Third-party code can register additional
analysis steps that run within the pipeline without modifying `profiler.py`.

## Graceful Degradation

Every step in the pipeline is wrapped in try/except. If a step fails, the profile still
completes with partial results rather than raising an exception.

```python
profile.degraded_features  # list[str] — steps that failed
profile.degradation_reasons  # dict[str, str] — step → error message
```

**What to check when a step degrades:**

| Degraded step | Common cause | What to do |
|---|---|---|
| `grain` | All columns have nulls or low cardinality | Supply `key_columns=` manually to `case_file` |
| `freshness` | No datetime columns detected | Add a temporal column or set `freshness_column=` explicitly |
| `duplicate_forensics` | `grain` also degraded | Fix grain first — duplicate analysis depends on it |
| `cross_column` | Very wide table (100+ columns) — sampled at 20K, may timeout | Reduce `level` to `"standard"` or use `context_columns=` to narrow |
| `value_stability` | No grain or grain is not unique | Resolve duplicates first |
| `join_profiler` | `reference_tables` DataFrame is empty or schema mismatch | Check that reference key columns exist in both tables |

A degraded profile is still useful. Read `findings` and `risks` — they often surface the
root cause of the degradation directly.

## Performance Characteristics

| Table Size | Level | Typical Duration |
|-----------|-------|------------------|
| 1K rows, 10 cols | quick | <50ms |
| 1K rows, 10 cols | standard | 100–300ms |
| 100K rows, 50 cols | standard | 1–3s |
| 1M rows, 100 cols | standard | 5–15s |
| 10M+ rows (Spark) | standard | 30–120s |

Performance bottlenecks by step:
- `grain_detector`: O(candidates × row_count) — capped at 10 single + 20 pair combinations
- `cross_column`: O(column_pairs × sample_rows) — sampled at 20K rows
- `value_stability`: O(grain_size × partition_count) — heavy on wide fact tables

**Accessing per-step timings:**

```python
profile = profile_table(df, subject="catalog.schema.orders")
print(profile.step_timings)
# {
#   "materialize": 210,        # Spark → pandas conversion (0 for native pandas)
#   "column_stats": 45,        # Step 1: stats_engine
#   "semantic_type": 12,       # Step 2: semantic_typer
#   "role_classification": 8,  # Step 3: role_classifier
#   "grain": 312,              # Step 4: grain_detector
#   "freshness": 18,           # Step 5: freshness
#   "classification": 5,       # Step 6: table_classifier
#   "format_issues": 22,       # Step 7: format_checker
#   "cleanliness": 19,         # Step 8: cleanliness
#   "duplicate_forensics": 88, # Step 9: duplicate_forensics
#   "string_length": 7,        # Step 10: string_length
#   "outlier_detection": 33,   # Step 11: outlier detection
#   "cross_column": 145,       # Step 12: cross_column
#   "value_stability": 204,    # Step 13: value_stability
# }
print(profile.profiling_duration_ms)  # total wall-clock (milliseconds)
```

Use `profile.step_timings` to identify which step is slow when the full profile takes
longer than expected. `materialize` is 0 for native pandas DataFrames.

**step_timings keys quick reference:**

| Key | Pipeline step | Notes |
|-----|---------------|-------|
| `materialize` | Spark → pandas conversion | `0` for native pandas |
| `column_stats` | Step 1: stats_engine | Always present |
| `semantic_type` | Step 2: semantic_typer | Always present |
| `role_classification` | Step 3: role_classifier | Always present |
| `grain` | Step 4: grain_detector | standard+ only |
| `freshness` | Step 5: freshness | standard+ only |
| `classification` | Step 6: table_classifier | standard+ only |
| `format_issues` | Step 7: format_checker | standard+ only |
| `cleanliness` | Step 8: cleanliness | standard+ only |
| `duplicate_forensics` | Step 9: duplicate_forensics | standard+ only |
| `string_length` | Step 10: string_length | standard+ only |
| `outlier_detection` | Step 11: outlier detection | standard+ only |
| `cross_column` | Step 12: cross_column | standard+ only |
| `value_stability` | Step 13: value_stability | standard+ only |

**Quality score thresholds** (maps `overall_quality_score` to `quality_summary`):

| Score range | `quality_summary` |
|-------------|-------------------|
| ≥ 0.90 | `"excellent"` |
| ≥ 0.75 | `"good"` |
| ≥ 0.50 | `"fair"` |
| < 0.50 | `"poor"` |

Score deductions: `−0.05 × affected_pct` per ERROR issue, `−0.02 × affected_pct` per WARNING,
`−0.10` for average null rate > 20%, `−0.05` for non-unique grain.

## Anchor Standard Output Contract

All three zoom levels produce output conforming to:

```python
{
    "kind": "profile_table" | "microscope" | "case_file",
    "subject": "<label>",
    "summary": "<one-line operational summary>",
    "metrics": {<flat numeric/bool values>},
    "findings": [<string observations>],
    "risks": [<actionable problems>],
    "samples": {<bounded sample collections>},
    "suggested_next_actions": [<what to do next>]
}
```

`profile_table` additionally includes `column_profiles` (a dict keyed by column name
with per-column statistics compatible with downstream tools like `suggest_rules`).
