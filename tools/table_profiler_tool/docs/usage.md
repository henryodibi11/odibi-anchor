# Table Profiler Tool — Usage

## profile_table

Full table profiling: shape, types, nulls, cardinality, grain, freshness, classification, and quality issues.

### Signature

```python
profile_table(
    df: DataFrame | str,
    subject: str,
    level: str = "standard",
    **opts,
) -> TableProfile
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | pandas/Spark DF | Yes | — | DataFrame to profile |
| `subject` | str | Yes | — | Identifying label (e.g. `"catalog.schema.table"`) |
| `level` | str | No | `"standard"` | Profiling depth: `"quick"`, `"standard"`, or `"deep"` |
| `**opts` | Any | No | — | `reference_tables=` to enable join profiling (see JoinProfile). Reserved for future extension otherwise. |

### Profiling Levels

| Level | Steps Run | Use Case |
|-------|-----------|----------|
| `quick` | Column stats, semantic type, role classification | Shape check, type survey |
| `standard` | All of quick + grain, freshness, classification, format checks, cleanliness, duplicate forensics, string length, outliers, cross-column, value stability | Default for first-look profiling |
| `deep` | Same as standard (all features enabled) | Future extension point for additional analyses |

### Return Value: TableProfile

| Field | Type | Description |
|-------|------|-------------|
| `subject` | str | Label passed in |
| `classification` | TableClassification | fact, dimension, snapshot, event_log, scd2, lookup, staging, aggregate, bridge, unknown |
| `classification_confidence` | float | 0.0–1.0 |
| `classification_reasoning` | str | Primary evidence sentence driving the classification |
| `classification_evidence` | list[str] | All evidence signals supporting the classification |
| `classification_counter_signals` | list[str] | Signals against the chosen classification |
| `classification_inference` | Inference | Full inference with evidence and runner-ups |
| `row_count` | int | Total rows |
| `column_count` | int | Total columns |
| `grain` | GrainAnalysis | Detected primary key; `best_grain`, `is_unique`, `duplicate_rate` |
| `duplicate_forensics` | DuplicateForensics | Where duplicates concentrate, verdict |
| `freshness` | FreshnessAnalysis | Timestamp column, staleness, cadence |
| `columns` | list[ColumnProfile] | Per-column deep profiles |
| `joins` | list[JoinProfile] | Joinability to reference tables (if `reference_tables` provided) |
| `format_issues` | list[FormatIssue] | Formatting/cleanliness problems |
| `outliers` | list[OutlierProfile] | Numeric outlier summaries |
| `overall_quality_score` | float | 0.0–1.0 composite quality score |
| `quality_summary` | str | Human label for quality score: `"excellent"`, `"good"`, `"fair"`, `"poor"` |
| `profiling_level` | str | Level that was actually run |
| `profiling_duration_ms` | int | Total wall-clock time |
| `step_timings` | dict[str, int] | Per-step millisecond timings (see architecture docs for key names) |
| `profiled_at` | str | ISO 8601 UTC timestamp of when the profile was computed |
| `degraded_features` | list[str] | Steps that failed gracefully |
| `degradation_reasons` | dict[str, str] | Step name → error message for each degraded step |
| `summary` | str | One-line operational summary |
| `findings` | list[str] | What the profiler discovered |
| `risks` | list[str] | Actionable problems |
| `actionable_risks` | list[ActionableRisk] | Structured risks with safe/unsafe actions |
| `suggested_actions` | list[str] | Recommended next steps |
| `suggested_sql` | list[str] | Generated SQL snippets for common remediation |

### ColumnProfile Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Column name |
| `position` | int | Zero-based column position in the DataFrame |
| `spark_type` | str | Original Spark/pandas dtype |
| `semantic_type` | SemanticType | email, phone, uuid, enum, etc. |
| `role` | ColumnRole | primary_key, measure, timestamp, etc. |
| `row_count` | int | Total rows |
| `non_null_count` | int | Total non-null rows |
| `null_count` / `null_pct` | int / float | Null statistics |
| `effective_null_pct` | float | Combined null + null-like rate (nulls + sentinel strings) |
| `null_like_count` | int | Count of string sentinel values (`''`, `'N/A'`, `'unknown'`, etc.) |
| `null_like_values` | list[str] | Which sentinel strings were found |
| `distinct_count` / `distinct_pct` | int / float | Cardinality |
| `is_unique` | bool | All values distinct |
| `is_constant` | bool | Single value |
| `top_values` | list[dict] | Most frequent values with counts |
| `sample_values` | list[str] | Representative samples |
| `min_value` / `max_value` | Any | Range endpoints |
| `mean_value` / `std_value` / `median_value` | float | Numeric stats |
| `min_length` / `max_length` / `avg_length` | int/float | String length stats |
| `pattern_fingerprint` | str | None | Dominant value fingerprint — use with `filter="pattern:XXX"` |
| `has_leading_spaces` / `has_trailing_spaces` | bool | Whitespace flags |
| `has_mixed_case` | bool | Enum case inconsistency |
| `has_embedded_units` | bool | Values like `"100 MW"` mixing text and numbers |
| `suspected_fk_target` | str | None | Inferred foreign key target column |
| `correlated_nulls` | list[str] | Columns whose nulls co-occur with this column's nulls |
| `format_issues` | list[FormatIssue] | Issues specific to this column |
| `quality_flags` | list[str] | Summary quality tags |

### GrainAnalysis

| Field | Type | Description |
|-------|------|-------------|
| `best_grain` | list[str] | Column(s) forming the primary key |
| `is_unique` | bool | Whether grain is perfectly unique |
| `duplicate_rate` | float | Fraction of rows that are duplicates |
| `candidates_tested` | list[dict] | All candidates evaluated with scores |
| `runner_up_grains` | list[dict] | Near-unique alternatives |
| `null_exclusion_rate` | float | Fraction of rows excluded because a grain column was null |
| `verification_hint` | str | Human-readable disambiguation hint when confidence is low |
| `duplicate_concentration` | str | None | Where duplicates concentrate (human-readable summary) |
| `inference` | Inference | Full inference metadata |

### FreshnessAnalysis

| Field | Type | Description |
|-------|------|-------------|
| `freshness_column` | str | Detected timestamp column |
| `latest_value` | str | Most recent timestamp |
| `earliest_value` | str | Oldest timestamp |
| `staleness` | str | Human label: `"fresh"`, `"stale"`, `"very_stale"` |
| `staleness_hours` | float | Hours since last record |
| `cadence` | str | Detected load frequency: `"daily"`, `"hourly"`, etc. |
| `avg_rows_per_period` | float | None | Average rows per cadence period |
| `last_load_row_count` | int | None | Row count of the most recent load period |
| `gap_detected` | bool | Whether a gap exists in the expected cadence |
| `gap_description` | str | None | Human-readable description of the gap (e.g. `"Missing 3 days between 2026-01-15 and 2026-01-18"`) |

### DuplicateForensics

Answers WHERE duplicates are concentrated and whether the pattern looks like intentional
snapshot design or a deduplication bug.

| Field | Type | Description |
|-------|------|-------------|
| `grain_columns` | list[str] | The columns used for duplicate detection |
| `duplicate_count` | int | Total duplicate rows |
| `duplicate_rate` | float | Fraction of rows that are duplicates |
| `concentration_column` | str | None | Column where duplicates concentrate most |
| `concentration_values` | list[dict] | Top concentrating values: `{"value": ..., "dup_count": ..., "dup_pct": ...}` |
| `is_time_concentrated` | bool | Whether duplicates cluster in a time window |
| `time_concentration_description` | str | None | e.g. `"80% of duplicates in last 3 days"` |
| `is_source_concentrated` | bool | Whether duplicates cluster in a source or category value |
| `source_concentration_description` | str | None | e.g. `"92% of duplicates where source='legacy_import'"` |
| `is_snapshot_pattern` | bool | Whether the pattern looks like intentional snapshot design |
| `verdict` | str | Duplicate pattern interpretation (see below) |
| `explanation` | str | Plain-language explanation of the verdict |

**Verdict values:**

| Verdict | Meaning |
|---------|---------|
| `"snapshot_design"` | Duplicates are concentrated by a time or partition column — this is intentional (SCD2, daily snapshots). Do not dedup without understanding the design. |
| `"dedup_needed"` | Duplicates appear to be unintentional — same business key appearing multiple times with no snapshot pattern. Run dedup before downstream use. |
| `"partial_overlap"` | Some duplicates are explainable (snapshot-like) but others look like errors — investigate concentration values before deciding. |
| `"unknown"` | Insufficient evidence to classify — grain may have been degraded or duplicate rate is very low. |

### JoinProfile

Populated when `reference_tables=` is passed to `profile_table`. Each entry describes a
potential join between a column in the profiled table and a reference table.

| Field | Type | Description |
|-------|------|-------------|
| `source_column` | str | Column in the profiled table |
| `target_table` | str | Reference table name |
| `target_column` | str | Matching column in the reference table |
| `cardinality` | str | Inferred join cardinality: `"many_to_one"`, `"one_to_one"`, `"many_to_many"` |
| `overlap_pct` | float | Fraction of source values found in the target |
| `orphan_count` | int | Source rows with no matching target row |
| `orphan_pct` | float | Fraction of source rows that are orphans |
| `format_compatible` | bool | Whether value formats align (e.g. catches `"001"` vs `1` mismatches) |
| `format_mismatch_description` | str | None | Describes the format incompatibility when `format_compatible=False` |
| `safe_join_type` | str | Recommended join type: `"LEFT"` (orphans present) or `"INNER"` (full overlap) |

**Usage:**
```python
ref = {"orders": spark.table("catalog.schema.dim_orders")}
profile = profile_table(df, subject="catalog.schema.facts", reference_tables=ref)
for jp in profile.joins:
    print(f"{jp.source_column} → {jp.target_table}.{jp.target_column}: "
          f"{jp.overlap_pct:.0%} overlap, {jp.orphan_count} orphans")
```

### OutlierProfile

One entry per numeric column where outliers were detected.

| Field | Type | Description |
|-------|------|-------------|
| `column` | str | Column name |
| `method` | str | Detection method: `"iqr"` (standard) or `"percentile"` (fallback when IQR=0, i.e. constant-dominated distributions) |
| `lower_bound` | float | Lower threshold — values below this are outliers |
| `upper_bound` | float | Upper threshold — values above this are outliers |
| `outlier_count` | int | Number of outlier rows |
| `outlier_pct` | float | Fraction of total rows that are outliers |
| `extreme_values` | list[dict] | Top outlier values with row index: `{"value": ..., "row_idx": ...}` |
| `context` | str | Human-readable description of the distribution pattern |

### FormatIssue

Produced by `format_checker`, `cleanliness`, and `string_length` steps. Each entry
describes one type of issue on one column.

| Field | Type | Description |
|-------|------|-------------|
| `issue_type` | str | Category: `"leading_spaces"`, `"trailing_spaces"`, `"mixed_case_enum"`, `"inconsistent_format"`, `"embedded_units"`, `"truncated_values"`, etc. |
| `column` | str | Affected column |
| `severity` | str | `"error"`, `"warning"`, or `"info"` |
| `description` | str | Human-readable description of the problem |
| `affected_count` | int | Number of affected rows |
| `affected_pct` | float | Fraction of rows affected |
| `examples` | list[str] | Sample affected values |
| `fix_suggestion` | str | Short description of the recommended fix |
| `safe_action` | str | What to do safely with these rows |
| `unsafe_action` | str | What breaks if you use the values as-is |
| `code_hint` | str | None | Generated code snippet for fixing the issue |

### ActionableRisk

Each entry in `profile.actionable_risks` is a structured risk with both the problem and remediation path.

| Field | Type | Description |
|-------|------|-------------|
| `description` | str | Plain-language risk description |
| `severity` | str | `"error"`, `"warning"`, or `"info"` |
| `safe_action` | str | Recommended remediation |
| `unsafe_action` | str | What breaks or goes wrong if the risk is ignored |
| `code_hint` | str | None | Generated code snippet addressing the risk |
| `columns_affected` | list[str] | Column(s) involved |
| `rows_affected_pct` | float | Fraction of rows affected by this risk |

---

## microscope

Single-column deep-dive: full distribution, pattern analysis, anomaly detection,
value clustering, and actionable recommendations.

### Signature

```python
microscope(
    df: DataFrame,
    column: str,
    *,
    subject: str | None = None,
    profile: TableProfile | None = None,
    sample_limit: int = 20,
    bin_count: int = 20,
) -> dict
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | pandas/Spark DF | Yes | — | DataFrame containing the column |
| `column` | str | Yes | — | Column name to investigate |
| `subject` | str | No | column name | Display label in output |
| `profile` | TableProfile | No | None | Pre-computed profile to avoid redundant stats |
| `sample_limit` | int | No | 20 | Max example values per category |
| `bin_count` | int | No | 20 | Number of histogram bins for numeric columns |

### Return Value (Anchor Standard Dict)

```python
{
    "kind": "microscope",
    "subject": "orders.amount",
    "summary": "<one-line column verdict>",
    "metrics": {
        "row_count": 1000,
        "null_count": 5,
        "null_pct": 0.005,
        "distinct_count": 800,
        "distinct_pct": 0.8,
        # ... type-specific metrics
    },
    "findings": ["Bimodal distribution detected", ...],
    "risks": ["3 extreme outliers beyond 3×IQR", ...],
    "samples": {
        "top_values": [...],
        "outlier_values": [...],
        "null_like_values": [...],
    },
    "suggested_next_actions": ["case_file(df, column='amount', filter='outliers')", ...]
}
```

Type-specific metrics:
- **Numeric**: histogram, IQR, skewness, kurtosis, outlier bounds
- **Categorical/String**: value frequencies, pattern fingerprints, cardinality analysis
- **Datetime**: time span, gaps, cadence, distribution over time

---

## case_file

Row-level investigation: shows actual rows matching a condition with full context
and co-occurrence analysis to explain WHY rows are broken.

### Signature

```python
case_file(
    df: DataFrame,
    *,
    column: str | None = None,
    filter: str | None = None,
    row_ids: list[Any] | None = None,
    key_columns: list[str] | None = None,
    subject: str | None = None,
    profile: TableProfile | None = None,
    limit: int = 20,
    context_columns: list[str] | None = None,
) -> dict
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | pandas/Spark DF | Yes | — | DataFrame to investigate |
| `column` | str | No | — | Column to focus on |
| `filter` | str | No | — | Smart filter string (see Filter Modes below) |
| `row_ids` | list | No | — | Explicit key values to look up. Format depends on key count — see Row ID Lookup below. |
| `key_columns` | list[str] | No | — | Columns that uniquely identify a row. Required with `row_ids`. Also used by `filter="duplicates"` when no profile is provided. |
| `subject` | str | No | auto | Display label |
| `profile` | TableProfile | No | None | Pre-computed profile for richer context |
| `limit` | int | No | 20 | Max rows returned |
| `context_columns` | list[str] | No | all | Columns to include in returned rows |

### Targeting Mode Precedence

`case_file` requires exactly one targeting mode. The order of precedence is:

1. `row_ids` (requires `key_columns`) — explicit row lookup by identity
2. `filter` (optionally paired with `column` or `key_columns`) — filter-based selection
3. `column` only — quick null check (see Column-Only Mode below)

If none are provided, raises `ValueError("Must provide at least one of: filter, row_ids, or column")`.

### Filter Modes

| Filter | Selects |
|--------|---------|
| `"nulls"` | Rows where `column` IS NULL |
| `"null_like"` | Rows where `column` is null-like: `''`, `'N/A'`, `'unknown'`, `'--'`, etc. |
| `"outliers"` | Rows where `column` value is an IQR outlier (1.5×IQR beyond Q1/Q3) |
| `"duplicates"` | Rows sharing a duplicate grain key. Uses `key_columns` if provided, otherwise falls back to `profile.grain.best_grain`. |
| `"top:N"` | Rows with the N most frequent values in `column` |
| `"bottom:N"` | Rows with the N least frequent values in `column` (rare/singleton) |
| `"pattern:XXX"` | Rows where `column` matches a specific value fingerprint (e.g. `"pattern:NNN-AAA"`) |
| `"format_issues"` | Rows where `column` does not match the dominant value pattern |
| `"where:EXPR"` | Rows matching an arbitrary pandas query expression (e.g. `"where:amount < 0"`) |

### Pandas Expression Power in `where:`

The `where:` prefix passes the expression directly to `df.query()`, which uses the Python
engine and supports calling any Series method on column names:

```python
# Null checks — equivalent to filter="nulls"
case_file(df, filter="where:status.isnull()")
case_file(df, filter="where:status.notna()")

# String methods
case_file(df, filter="where:name.str.contains('ERROR', case=False)")
case_file(df, filter="where:code.str.startswith('TMP')")
case_file(df, filter="where:description.str.len() > 500")

# Datetime accessor
case_file(df, filter="where:created_at.dt.year == 2023")
case_file(df, filter="where:created_at.dt.dayofweek == 6")  # Sundays

# Numeric checks
case_file(df, filter="where:amount.between(0, 0.01)")
case_file(df, filter="where:price < 0")

# Compound conditions
case_file(df, filter="where:status == 'active' and region != 'East'")
case_file(df, filter="where:amount < 0 or amount > 100000")
case_file(df, filter="where:category in ['A', 'B']")
```

> **`@variable` gotcha**: `df.query()` supports `@varname` to reference local Python variables,
> but this does NOT work through `where:` because the expression string has no access to your
> calling scope. Embed thresholds as literals: use `"where:amount > 1000"` not `"where:amount > @threshold"`.

### Row ID Lookup

Use `row_ids` + `key_columns` to fetch specific rows by identity value rather than a filter condition.

**Single key column** — pass a flat list of scalar values:

```python
case_file(df, key_columns=["order_id"], row_ids=[101, 102, 103])
```

**Multiple key columns** — pass a list of **tuples**, one per row, with values positionally aligned to `key_columns`:

```python
case_file(
    df,
    key_columns=["order_id", "line_item"],
    row_ids=[(101, 1), (101, 2), (202, 1)],
)
```

Each tuple `(101, 1)` resolves to `order_id=101 AND line_item=1`. The order of values inside each tuple must match the order of columns in `key_columns`.

> **⚠️ Silent-skip gotcha:** With multiple key columns, any entry in `row_ids` that is not a `list` or `tuple` is silently ignored — it does not raise an error and matches no rows. If your lookup returns fewer rows than expected, check that you are passing tuples, not scalars.

**Using `profile.grain.best_grain` as key columns:**

`best_grain` from `profile_table` is a natural source for `key_columns`:

```python
profile = anchor("profile_table", df, subject="catalog.schema.orders")
grain = profile.grain.best_grain           # e.g. ["order_id", "line_item"]

# Single key — flat list
case_file(df, key_columns=grain, row_ids=[101, 102])

# Composite key — list of tuples
case_file(df, key_columns=grain, row_ids=[(101, 1), (101, 2)])
```

### Column-Only Mode (Quick Quality Check)

When only `column=` is provided (no `filter`, no `row_ids`), `case_file` performs a
quick null check:

- If the column **has nulls** → returns rows where the column IS NULL (same as `filter="nulls"`)
- If the column **has no nulls** → returns all rows with `filter_desc="all_rows"` (fallback)

```python
# Equivalent to filter="nulls" when nulls exist
case_file(df, column="customer_id")

# Combine with context_columns to limit output width
case_file(df, column="customer_id", context_columns=["order_id", "customer_id", "status"])
```

This mode is useful for a quick sanity check when you don't yet know what to filter on.

### Return Value (Anchor Standard Dict)

```python
{
    "kind": "case_file",
    "subject": "orders.customer_id nulls",
    "summary": "5 rows where customer_id IS NULL",
    "metrics": {
        "matched_rows": 5,
        "total_rows": 1000,
        "matched_pct": 0.005,
    },
    "findings": ["All null customer_id rows also have status='inactive'", ...],
    "risks": ["Null customer_id rows may break downstream joins", ...],
    "samples": {
        "rows": [{"order_id": 3, "customer_id": None, "status": "inactive"}, ...],
        "co_occurrence": [{"column": "status", "value": "inactive", "pct": 1.0}, ...]
    },
    "suggested_next_actions": ["Filter these rows before join", ...]
}
```

### Co-Occurrence Analysis

`case_file` automatically checks other columns in the matched rows for patterns:
- If 50%+ of flagged rows share a particular value in another column, it reports the co-occurrence
- Uses lift filtering (1.5x above table baseline) to avoid reporting table-wide constants
- Skips columns where the mode value appears in 90%+ of the full table (no signal)
- Checks top 20 low-cardinality columns (sorted by `nunique` ascending for strongest signal first)
- Samples at 10K rows if the matched set is large

---

## Renderers

| Function | Input | Output | Token Budget |
|----------|-------|--------|--------------|
| `render_table_ai_summary(profile, max_cols=10)` | TableProfile | Compact YAML-like text | ~500 tokens |
| `render_table_profile_md(profile)` | TableProfile | Full GFM markdown report | ~2000+ tokens |
| `render_microscope_md(result)` | microscope dict | Markdown column report | ~500 tokens |
| `render_case_file_md(result)` | case_file dict | Markdown row report | ~300 tokens |

---

## Serialization

`serialize_profile(profile, sample_limit=10)` converts a `TableProfile` to the
Anchor standard output contract dict. This is what the `anchor()` dispatcher calls internally.

The serialized dict includes a `column_profiles` section (dict keyed by column name)
compatible with downstream tools like `suggest_rules`.

---

## Enums Reference

### TableClassification

| Value | Description |
|-------|-------------|
| `fact` | Transaction/event table with measures |
| `dimension` | Descriptive attributes for joins |
| `snapshot` | Point-in-time copies of state |
| `event_log` | Append-only event stream |
| `scd2` | Slowly changing dimension type 2 |
| `lookup` | Small reference/mapping table |
| `staging` | Intermediate/raw landing table |
| `aggregate` | Pre-computed summary table |
| `bridge` | Many-to-many relationship resolver |
| `unknown` | Cannot classify with confidence |

### ColumnRole

| Value | Description |
|-------|-------------|
| `primary_key` | Unique row identifier |
| `natural_key` | Business-meaningful key |
| `surrogate_key` | System-generated key |
| `foreign_key` | Reference to another table |
| `measure` | Numeric value for aggregation |
| `dimension` | Categorical attribute for grouping |
| `flag` | Boolean/binary indicator |
| `timestamp` | Date/time column |
| `partition` | Partition column |
| `derived` | Computed from other columns |
| `metadata` | System metadata (created_at, etc.) |
| `freetext` | Unstructured text |
| `identifier` | Non-key identifier (e.g. name) |
| `unknown` | Cannot classify |

### SemanticType

| Value | Description |
|-------|-------------|
| `email` | Email addresses |
| `phone` | Phone numbers |
| `url` | URLs |
| `uuid` | UUID/GUID values |
| `ip_address` | IPv4/IPv6 addresses |
| `zip_code` | Postal codes |
| `state_code` | US state abbreviations |
| `country_code` | ISO country codes |
| `currency_amount` | Dollar amounts with symbols |
| `percentage` | Percentage values |
| `date_string` | Date values stored as strings |
| `boolean_string` | "true"/"false"/"yes"/"no" |
| `json_string` | JSON-encoded strings |
| `delimited_list` | Pipe/comma separated lists |
| `file_path` | File system paths |
| `enum` | Low-cardinality categorical |
| `code` | Short codes (status, category) |
| `freetext` | Long unstructured text |
| `numeric_string` | Numbers stored as strings |
| `unknown` | Cannot classify |
