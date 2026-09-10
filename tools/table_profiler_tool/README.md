# Table Profiler

> Understand any DataFrame in one call: shape, types, nulls, cardinality, grain, freshness, and quality issues.

This tool covers three actions at different zoom levels:

| Action | Question | When to call |
|--------|----------|--------------|
| `profile_table` | What IS this table? | First look at any new table |
| `microscope` | What's going on in this column? | After profile flags a column |
| `case_file` | Show me the actual broken rows | After microscope identifies an issue |

---

## When to Use

- You have a new table you have never seen before — start with `profile_table`
- A pipeline just loaded data and you want to sanity-check it before writing downstream
- You need to understand cardinality, grain, or data quality before writing a join or aggregate
- A column is flagged as high-null or high-cardinality and you want the full distribution — use `microscope`
- You know rows are broken and need to see the actual data to understand why — use `case_file`

**Anti-pattern:** Don't use `profile_table` when you already know which column has the problem — go straight to `microscope`. Don't write `df.value_counts()` or `df[df["col"].isna()]` — `microscope` and `case_file` give structured output with actionable context.

---

## Quick Start

```python
import pandas as pd

# Inline sample data — replace with your own DataFrame
df = pd.DataFrame({
    "order_id":    [1, 2, 2, 3, 4],
    "customer_id": [101, 102, 102, None, 104],
    "amount":      [150.0, 200.0, 200.0, -50.0, 0.0],
    "status":      ["active", "active", "active", "inactive", "active"],
})

# Profile the DataFrame
ctx = anchor("profile_table", df, subject="orders_sample")

# Deep-dive a flagged column (amount has a negative value and a duplicate)
result = anchor("microscope", df, "amount", subject="orders.amount")

# See the rows where customer_id is null
rows = anchor("case_file", df, column="customer_id", filter="nulls")
```

---

## Usage

### profile_table

#### Basic
```python
ctx = anchor("profile_table", df, subject="orders")
```

#### With depth level
```python
# "quick" — shape, types, null counts only (fastest)
ctx = anchor("profile_table", df, subject="orders", level="quick")

# "standard" — adds grain detection, freshness, format checks (default)
ctx = anchor("profile_table", df, subject="orders", level="standard")

# "deep" — adds cross-column dependencies, value stability (slowest)
ctx = anchor("profile_table", df, subject="orders", level="deep")
```

#### Markdown output
```python
report = anchor("profile_table", df, subject="orders", output_format="markdown")
print(report)
```

### microscope

#### Basic
```python
result = anchor("microscope", df, "amount")
```

#### With options
```python
result = anchor("microscope", df, "amount",
            subject="orders.amount",
            sample_limit=50,
            bin_count=30)
```

#### Reuse an existing profile
```python
profile_ctx = anchor("profile_table", df, subject="orders")
result = anchor("microscope", df, "amount", profile=profile_ctx)
```

### case_file

#### Filter modes
```python
anchor("case_file", df, column="amount",   filter="nulls")           # IS NULL
anchor("case_file", df, column="status",   filter="null_like")       # '', 'N/A', 'unknown', '--'
anchor("case_file", df, column="amount",   filter="outliers")        # IQR outliers
anchor("case_file", df, column="order_id", filter="duplicates")      # duplicate key rows
anchor("case_file", df, column="status",   filter="top:5")           # 5 most frequent values
anchor("case_file", df, column="status",   filter="bottom:5")        # 5 rarest values
anchor("case_file", df, column="amount",   filter="where:amount < 0") # arbitrary expression
```

#### Limit returned columns
```python
anchor("case_file", df, column="amount", filter="outliers",
   context_columns=["order_id", "amount", "created_at"])
```

#### Look up specific rows by key
```python
anchor("case_file", df, row_ids=["ORD-001", "ORD-002"], key_columns=["order_id"])
```

### Direct Import (no anchor() dispatcher needed)

Call the underlying functions directly for standalone scripts or zero-overhead usage:

```python
import sys
import pandas as pd
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.table_profiler_tool.lib.profiler import profile_table
from tools.table_profiler_tool.lib.microscope import microscope
from tools.table_profiler_tool.lib.case_file import case_file

df = pd.DataFrame({
    "order_id":    [1, 2, 2, 3, 4],
    "customer_id": [101, 102, 102, None, 104],
    "amount":      [150.0, 200.0, 200.0, -50.0, 0.0],
    "status":      ["active", "active", "active", "inactive", "active"],
})

# profile_table returns a TableProfile object (not the dict contract).
# Pass it to microscope/case_file via profile= to avoid re-profiling.
profile = profile_table(df, subject="orders_sample")

# microscope and case_file return the standard Anchor dict contract directly
col_result = microscope(df, "amount", subject="orders.amount")
print(col_result["summary"])

rows_result = case_file(df, column="customer_id", filter="nulls")
print(rows_result["summary"])
```

---

## Parameters

### profile_table

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | DataFrame or str | ✓ | — | DataFrame to profile, or Unity Catalog table name |
| `subject` | str | | `"dataframe"` | Display name in output |
| `level` | str | | `"standard"` | `"quick"`, `"standard"`, or `"deep"` |
| `output_format` | str | | `"dict"` | `"dict"` or `"markdown"` |

### microscope

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | DataFrame | ✓ | — | DataFrame containing the column |
| `column` | str | ✓ | — | Column name to investigate |
| `subject` | str | | column name | Display label in output |
| `profile` | TableProfile | | None | Pre-computed profile (avoids redundant stats) |
| `sample_limit` | int | | 20 | Max example values per category |
| `bin_count` | int | | 20 | Histogram bins for numeric columns |

### case_file

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | DataFrame | ✓ | — | DataFrame to investigate |
| `column` | str | | — | Column to focus on |
| `filter` | str | | — | Smart filter string (see filter modes above) |
| `row_ids` | list | | — | Explicit key values to look up |
| `key_columns` | list[str] | | — | Row identity columns (required with `row_ids`) |
| `subject` | str | | auto | Display label |
| `profile` | TableProfile | | None | Pre-computed profile for richer context |
| `limit` | int | | 20 | Max rows to return |
| `context_columns` | list[str] | | all | Columns to include in returned rows |

---

## Documentation

* [Architecture](docs/architecture.md) — System overview, module dependency graph, design decisions
* [Usage](docs/usage.md) — Full parameter reference, return types, enum values
* [Code Walkthrough](docs/code_walkthrough.md) — Worked examples with real data traces

---

## Output

### profile_table — Key Fields

| Field | Type | What It Tells You |
|-------|------|-------------------|
| `summary` | str | One-line verdict: row count, quality, and top risk |
| `metrics.row_count` | int | Total rows profiled |
| `metrics.health_score` | float | 0.0–1.0 quality score |
| `classifications.table_type` | str | `"fact"`, `"dimension"`, `"lookup"`, `"staging"`, `"unknown"` |
| `grain_analysis.grain_columns` | list[str] | Detected primary key columns. Empty = no unique key found |
| `freshness_analysis` | dict | Latest timestamp column and estimated lag |
| `column_profiles` | list | Per-column stats |
| `findings` | list | What the tool discovered |
| `risks` | list | Actionable problems |
| `suggested_next_actions` | list | What to do next |

### microscope — Key Fields

| Field | Type | What It Tells You |
|-------|------|-------------------|
| `summary` | str | One-line verdict for this column |
| `metrics.null_count` | int | Null rows |
| `metrics.distinct_count` | int | Unique non-null values |
| `distribution` | dict | Histogram (numeric) or value frequency table (categorical) |
| `patterns` | list | Detected format patterns (e.g. `"YYYY-MM-DD"`) |
| `anomalies` | list | Outlier values and unexpected formats |
| `suggested_next_actions` | list | What to do next |

### case_file — Key Fields

| Field | Type | What It Tells You |
|-------|------|-------------------|
| `summary` | str | How many rows matched and why |
| `metrics.matched_count` | int | Rows matching the filter |
| `metrics.matched_pct` | float | Share of total rows matched |
| `rows` | list[dict] | Actual row data (up to `limit`) |
| `co_occurrences` | list | Other columns that concentrate in the flagged rows |
| `suggested_next_actions` | list | Concrete next steps |

### Example Output

```python
ctx = anchor("profile_table", df, subject="orders")

ctx["summary"]
# "orders: 1,204,311 rows, 18 columns, health_score=0.72 — 2 high-null columns"

ctx["grain_analysis"]["grain_columns"]
# ["order_id"]

ctx["suggested_next_actions"]
# ["anchor('microscope', df, 'amount') — 23.4% null, investigate distribution"]
```

---

## Reading the Output

### What to look for first
1. `summary` — the one-line answer
2. `risks` — anything blocking further work?
3. `suggested_next_actions` — what the tool recommends

### Common findings and what they mean

| Finding | What It Means | What To Do |
|---------|--------------|------------|
| `health_score < 0.8` | Quality issues detected | Check `risks` for specifics |
| `grain_columns = []` | No unique key detected | Verify expected grain before joining or aggregating |
| High null rate on column X | >30% values missing | Check upstream source or pipeline logic |
| `co_occurrences` in case_file | Other columns cluster with broken rows | Investigate those columns too |
| `anomalies` in microscope | Unexpected values or formats | May need normalization before use |

---

## Pairs Well With

| If you found... | Then run... | Why |
|-----------------|-------------|-----|
| High-null or high-cardinality column | `anchor("microscope", df, "col")` | Full distribution and pattern analysis |
| An anomaly in microscope | `anchor("case_file", df, column="col", filter="outliers")` | See the actual broken rows |
| Quality issues before a join | `anchor("pre_join", left_df, right_df, keys=["id"])` | Verify join is safe before writing it |
| Ready to clean the data | `anchor("transform", profile_ctx)` | Auto-generate a cleanup plan |
| Need formal validation rules | `anchor("suggest_rules", df, subject="orders")` | Generate rules from the profile |
