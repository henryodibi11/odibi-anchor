# Table Profiler Reference

> Preserved profiling technique.

Three tools that replace 30 minutes of manual investigation with a 5-second deterministic answer.

**Load this skill when:**
- Exploring an unfamiliar table or DataFrame
- Assessing data quality before a pipeline write
- Investigating nulls, outliers, duplicates, or format issues
- Drilling into a specific column's distribution
- Debugging specific problematic rows
- Running any `anchor("profile_table")`, `anchor("microscope")`, or `anchor("case_file")` call

## The Investigation Chain

These tools form a drill-down workflow. Each tool's `suggested_next_actions` tells you what to call next.

```
profile_table  →  "What's this table? What's wrong?"
     │
     ▼ (finding: "county has 10.9% nulls")
microscope     →  "Tell me everything about this column"
     │
     ▼ (finding: "1214 outliers detected")
case_file      →  "Show me those specific rows — WHY are they broken?"
```

**Rule:** Don't jump to case_file without context. Profile first, then drill.

## Tool Reference

### 1. `anchor("profile_table")` — Full Structural Profile

Answers: What IS this table? What's WRONG? What should I DO next?

```python
# By table name (auto-reads via spark.table)
anchor("profile_table", "catalog.schema.table")

# By DataFrame
anchor("profile_table", df, subject="my_table")

# Output formats
anchor("profile_table", df, output_format="dict")        # Anchor contract (default)
anchor("profile_table", df, output_format="markdown")    # Full GFM report
anchor("profile_table", df, output_format="ai_summary")  # Compact LLM context
```

**Output (dict mode):** Anchor standard contract with:
- `metrics`: quality_score, table_type, grain, row_count, column_count, freshness
- `findings`: Actionable issues (nulls, outliers, format inconsistencies, duplicates)
- `risks`: Data quality risks ranked by severity
- `suggested_next_actions`: Exact anchor() calls to run next

**Performance:** ~5s for 20K rows × 28 cols. Scales linearly with column count.

---

### 2. `anchor("microscope")` — Column Deep-Dive

Answers: What's the full story on this one column?

```python
anchor("microscope", df, "column_name")
anchor("microscope", df, "column_name", output_format="dict")
anchor("microscope", df, "column_name", output_format="markdown")
anchor("microscope", df, "column_name", sample_limit=30, bin_count=25)
```

**Output varies by detected column type:**

| Type | Key Metrics | What to Look For |
|------|------------|------------------|
| **numeric** | min, max, mean, std, histogram, outlier_count, zero_count | Skewness, IQR outliers, negative values |
| **string** | distinct_count, top_values, pattern_fingerprints, null_like_count, whitespace_issues | Format variants, null sentinels ('N/A', '--'), leading/trailing spaces |
| **timestamp** | earliest, latest, span_days, inferred_cadence, future_date_count | Gaps, wrong timezone, future dates |
| **boolean** | true_count, false_count, null_pct | Imbalanced flags |

**Pattern fingerprints** (string columns): Replaces digits with `9`, letters with `A`.
Example: `"PJM-B16"` → `"AAA-A99"`. Shows format variants at a glance.

**Performance:** <0.5s on 134K rows.

---

### 3. `anchor("case_file")` — Row Investigation

Answers: Show me the actual broken rows. WHY are they broken?

```python
# Smart filters (pick ONE)
anchor("case_file", df, column="county", filter="nulls")
anchor("case_file", df, column="col", filter="null_like")       # catches '', 'N/A', 'null', '--'
anchor("case_file", df, column="amount", filter="outliers")      # IQR-based
anchor("case_file", df, filter="duplicates", key_columns=["id", "date"])
anchor("case_file", df, column="fuel_type", filter="top:3")      # most frequent N values
anchor("case_file", df, column="fuel_type", filter="bottom:2")   # rarest N values
anchor("case_file", df, column="date_col", filter="pattern:9/99/9999")  # specific format
anchor("case_file", df, filter="where:mw_capacity > 500")        # arbitrary expression

# Row lookup by ID
anchor("case_file", df, row_ids=[101, 102], key_columns=["order_id"])

# Control output
anchor("case_file", df, column="x", filter="nulls", limit=50)    # more rows
anchor("case_file", df, column="x", filter="nulls", context_columns=["id", "x", "status"])
anchor("case_file", df, column="x", filter="nulls", output_format="markdown")
```

**Filter types:**

| Filter | Requires | What it finds |
|--------|----------|---------------|
| `nulls` | column | Rows where column IS NULL |
| `null_like` | column | Rows where column is null-like ('', 'N/A', 'null', 'None', '-', '--') |
| `outliers` | column (numeric) | Rows outside IQR bounds (Q1-1.5×IQR, Q3+1.5×IQR) |
| `duplicates` | key_columns OR profile | Rows sharing a duplicate grain key |
| `top:N` | column | Rows with the N most frequent values |
| `bottom:N` | column | Rows with the N rarest values (singletons/rare) |
| `pattern:XXX` | column (string) | Rows matching a specific fingerprint pattern |
| `where:EXPR` | — | Arbitrary pandas query expression |

**Co-occurrence detection:** When rows match a filter, case_file checks whether
other columns are unusually concentrated in the flagged subset vs the full table.
Uses **lift-based** filtering (concentration ≥50% AND lift ≥1.5× above baseline).
Table-wide constants are automatically excluded.

Co-occurrence output includes:
- `column`: The co-occurring column
- `value`: The dominant value in flagged rows
- `pct`: Concentration in flagged rows
- `lift`: How much more concentrated vs full table (1.5× = minimum signal)
- `baseline_pct`: The value's frequency in the full table

**Performance:** <0.2s on 134K rows.

---

## Interpreting Output

### Anchor Contract Shape (all 3 tools)

```python
{
    "kind": "profile_table" | "microscope" | "case_file",
    "subject": "table_or_column_name",
    "summary": "One-line human-readable conclusion",
    "metrics": { ... },           # Quantitative facts
    "findings": ["...", ...],     # Actionable observations
    "risks": ["...", ...],        # Severity-ranked concerns
    "samples": { ... },           # Evidence (rows, top_values, histograms)
    "suggested_next_actions": ["...", ...],  # Often contains exact anchor() calls
}
```

### Following suggested_next_actions

The output's `suggested_next_actions` field often contains the literal next call:
```
"Run anchor('microscope', df, 'county') for full column analysis"
"Run anchor('case_file', df, column='county', filter='nulls') to investigate"
```

Follow these — they're generated based on the actual findings.

### Quality Score Interpretation (profile_table)

| Score | Rating | Meaning |
|-------|--------|----------|
| ≥ 0.95 | Excellent | Production-ready |
| 0.85-0.95 | Good | Minor issues, usable |
| 0.70-0.85 | Fair | Needs attention before downstream use |
| < 0.70 | Poor | Significant quality problems |

### Lift Interpretation (case_file co-occurrence)

| Lift | Meaning |
|------|----------|
| 1.0× | Same as full table — no signal |
| 1.5× | Minimum threshold — mild signal |
| 2.0-3.0× | Clear signal — worth reporting |
| >5.0× | Strong signal — almost certainly explains the issue |

## Common Patterns

### Pattern 1: Quick Table Assessment
```python
result = anchor("profile_table", "catalog.schema.table", output_format="dict")
# → Check result["metrics"]["quality_score"], result["findings"]
```

### Pattern 2: Null Investigation Workflow
```python
# 1. Profile finds nulls
profile = anchor("profile_table", df, output_format="dict")
# → finding: "county has 10.9% nulls"

# 2. Microscope the column
micro = anchor("microscope", df, "county", output_format="dict")
# → null_pct, null_like_count, pattern_fingerprints

# 3. Investigate the null rows
cf = anchor("case_file", df, column="county", filter="nulls", output_format="dict")
# → co-occurrences explain WHY (e.g., all from one market)
```

### Pattern 3: Pre-Write Quality Gate
```python
result = anchor("profile_table", df_to_write, subject="target_table", output_format="dict")
if result["metrics"]["quality_score"] < 0.85:
    print(f"Quality {result['metrics']['quality_score']} — review findings:")
    for f in result["findings"]:
        print(f"  - {f}")
    # Don't write until issues resolved
```

### Pattern 4: Outlier Deep-Dive
```python
micro = anchor("microscope", df, "revenue", output_format="dict")
# → metrics["outlier_count"] = 47
cf = anchor("case_file", df, column="revenue", filter="outliers", output_format="dict")
# → Shows the 47 rows, co-occurrence reveals they're all from one client
```

### Pattern 5: Duplicate Grain Verification
```python
cf = anchor("case_file", df, filter="duplicates", key_columns=["id", "date"], output_format="dict")
# → metrics["matched_rows"] = 0 means grain is clean
# → matched_rows > 0 means grain violation — check the rows
```

### Pattern 6: Diff → Coerce Check → Coerce Fix
```python
# 1. Diff shows WHAT changed — now with NULL-aware breakdown
ctx = anchor("diff", old_df, new_df, keys=["Application ID"])
# → changed_column_counts now includes null_to_value, value_to_null, value_changed per column
# → Immediately see: is this a null-fill, a deletion, or a value change?

# 2. For columns with value_changed > 0, check WHY they differ
coerce_ctx = anchor("coerce_check", old_df, new_df, keys=["Application ID"],
         columns=["Interconnection Entity", "Generic Queue Status"])
# → Classifies each mismatch: whitespace, case, unicode, numeric_representation, date_format, or genuine
# → dominant_category tells you the fix: TRIM(), UPPER(), strip zero-width chars, CAST, etc.
# → Only compares non-null pairs; null transitions are already visible in diff

# 3. Generate fix plan from coerce_check findings, then apply transforms
fix_plan = anchor("transform", coerce_ctx)  # Generates transform plan from findings
cleaned_df = anchor("apply_transform", dirty_df, fix_plan)
# → Applies: TRIM+collapse, UPPER/LOWER, strip zero-width chars, normalize numbers, parse dates
# → Skips "genuine" columns automatically
# → Returns fixed DataFrame + before/after samples
# → Review fix_plan first to see what will change

# 4. Verify: re-run coerce_check on fixed data
anchor("coerce_check", old_df, cleaned_df, keys=["Application ID"],
   columns=["Interconnection Entity", "Generic Queue Status"])
# → Should show 0 mismatches (or only genuine ones remaining)
```

**When to use coerce_check + transform/apply_transform:** After `diff` shows value_changed > 0 and you suspect
the differences are formatting, not real data changes. Common with CRM/queue data where source
systems coerce differently. `transform` + `apply_transform` automates the manual TRIM/UPPER/strip code.

**Important:** Pass the DataFrame with the formatting issues to `apply_transform`. If 0 rows are
affected, the tool warns you — try the other side.

### Pattern 7: Pre-Join Validation
```python
# Before joining two tables, validate join keys on both sides
anchor("validate", fact_df, rules=[{"column": "project_id", "rule": "not_null"}])
anchor("validate", dim_df, rules=[{"column": "project_id", "rule": "not_null"}])

# Check key cardinality and overlap using microscope
anchor("microscope", fact_df, "project_id")  # distinct count, null pct, top values
anchor("microscope", dim_df, "project_id")   # compare — do values overlap?

# For composite keys, validate each component
anchor("validate", orders_df, rules=[
    {"column": "product_id", "rule": "not_null"},
    {"column": "warehouse_id", "rule": "not_null"},
])

# Check for duplicates that would cause fanout
anchor("duplicate", dim_df, ["project_id"])  # 1:many risk if duplicates exist
```

**Workflow:** Validate keys (not_null, unique) → microscope both sides (overlap, cardinality) →
duplicate check (fanout risk). Follow `suggested_next_actions` — common next steps include
`anchor("microscope")` for suspicious keys or `anchor("case_file")` for orphan investigation.

### Pattern 8: Profile → Derive Validation Rules
```python
# 1. Profile the table
profile_ctx = anchor("profile_table", df, subject="orders", output_format="dict")

# 2. Derive rules from profile findings:
#    - null_pct=0 → not_null rule
#    - distinct_pct=1.0 → unique rule  
#    - low cardinality → accepted_values rule
#    - numeric columns → range rule from min/max
rules = [
    {"column": "order_id", "rule": "not_null"},
    {"column": "order_id", "rule": "unique"},
    {"column": "status", "rule": "accepted_values", "values": ["Active", "Pending", "Closed"]},
    {"column": "amount", "rule": "range", "min": 0, "max": 1000000},
]

# 3. Validate against the derived rules
anchor("validate", df, rules=rules)
```

**Workflow:** Profile gives you the evidence (null_pct, distinct_count, min/max) — you translate
findings into explicit rules. This is more transparent than auto-generation and gives you
control over strictness. Use `profile_ctx["findings"]` to identify which columns need rules.

### Pattern 9: Delta Version Comparison
```python
# Compare two Delta table versions by reading them as versioned DataFrames
old_df = spark.read.format("delta").option("versionAsOf", 10).table("catalog.schema.table")
new_df = spark.read.format("delta").option("versionAsOf", 12).table("catalog.schema.table")

# Use anchor("diff") for row-level comparison
anchor("diff", old_df, new_df, keys=["project_id"])
# → Row-level diff: inserts, updates, deletes between versions
# → Column-level breakdown: which columns changed and how

# Check Delta history for operation metadata (who, when, what)
# DESCRIBE HISTORY catalog.schema.table LIMIT 10;

# Markdown output
anchor("diff", old_df, new_df, keys=["id"], output_format="markdown")
```

**Workflow:** Read specific versions with `versionAsOf` → pass to `anchor("diff")` for comparison.
Use `DESCRIBE HISTORY` to find relevant version numbers first.

### Pattern 10: Row Lineage Tracing
```python
# Trace a single row's lineage through upstream sources
anchor("trace_row", output_df, keys=["project_id"], values={"project_id": "PJM-12345"},
   upstream={"source": source_df, "dim": dim_df})
# → Traces each output column back to its upstream source
# → Use when tracing where a wrong value came from
```

### Pattern 11: Schema Evolution
```python
# Apply schema evolution and fix type mismatches systematically
anchor("evolve", df, target_schema=target_df, subject="orders")
# → Composed workflow: schema_diff → schema_migrate → coerce_fix → validate
# → Use when schema_diff shows structural changes or coerce_check reveals type issues

# Or start from a coerce_check result
anchor("evolve", df, coerce_ctx=coerce_check_result)
```

## Gotchas

1. **output_format defaults to markdown** in Anchor dispatcher. Use `output_format="dict"` for programmatic access.
2. **Spark DataFrames are converted to pandas** internally. For tables >500K rows, the tools sample automatically.
3. **profile_table subject parameter**: Pass it when using a DataFrame (not a table name) so output labels are meaningful.
4. **case_file requires at least ONE targeting mode**: column+filter, row_ids+key_columns, or filter alone (for duplicates/where).
5. **pattern fingerprint format**: Digits→`9`, uppercase→`A`, lowercase→`a`. So `"2024-01-15"` → `"9999-99-99"`, `"PJM-B16"` → `"AAA-A99"`.
6. **Co-occurrence returns empty for diverse tables**: This is correct behavior — diverse multi-category data genuinely has no strong co-occurrence. Don't treat empty co-occurrence as a bug.
7. **where filter uses pandas query syntax**: Column names with spaces need backticks: `` filter="where:`Column Name` > 100" ``

## Source Code Location

```
tools/table_profiler_tool/
├── lib/
│   ├── profiler.py          # profile_table() orchestrator
│   ├── microscope.py        # microscope() column deep-dive
│   ├── case_file.py         # case_file() row investigation
│   ├── contract.py          # serialize_profile/microscope/case_file
│   ├── renderer.py          # render_*_md() markdown formatters
│   ├── models.py            # TableProfile, ColumnProfile, etc.
│   └── ...                  # 15+ analysis modules
└── tests/
    ├── test_profiler.py     # 86 tests
    ├── test_microscope.py   # 19 tests
    ├── test_case_file.py    # 19 tests
    └── ...                  # 576 total tests
```

Anchor dispatcher wrappers: `src/odibi_anchor/_dispatcher/_tool_wrappers.py` (registered in `bootstrap.py`).
