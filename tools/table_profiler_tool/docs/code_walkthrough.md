# Table Profiler Tool — Code Walkthrough

## Worked Example 1: profile_table Pipeline

Input: A pandas DataFrame with 1,000 rows and 5 columns.

```python
import pandas as pd
import numpy as np

df = pd.DataFrame({
    "order_id": range(1, 1001),
    "customer_id": np.random.randint(100, 200, 1000),
    "amount": np.random.uniform(10.0, 500.0, 1000),
    "status": np.random.choice(["active", "inactive", "pending"], 1000),
    "created_at": pd.date_range("2026-01-01", periods=1000, freq="h"),
})
```

### Step-by-step execution trace:

**1. Engine detection** (`_sampling.py`)
```
detect_engine(df) → "pandas"
```
Checks `type(df).__module__` — if it starts with `pyspark`, returns `"spark"`, otherwise `"pandas"`.

**2. Shape**
```
row_count = 1000, column_count = 5
```

**3. Early materialization check**
```
1000 ≤ 500,000 AND 5 ≤ 100 → already pandas, skip
```
For a Spark DataFrame of this size, it would call `df.toPandas()` here and preserve
the original Spark type names in `_spark_schema`.

**4. Step 1: stats_engine** → `compute_column_stats(df)`

Iterates each column and produces a `ColumnProfile`:
```
ColumnProfile(name="order_id", spark_type="int64", row_count=1000,
    null_count=0, null_pct=0.0, distinct_count=1000, distinct_pct=1.0,
    is_unique=True, min_value=1, max_value=1000, mean_value=500.5,
    top_values=[{"value": 1, "count": 1}, ...],
    sample_values=["1", "42", "789", ...])

ColumnProfile(name="customer_id", spark_type="int64", row_count=1000,
    null_count=0, distinct_count=~95, distinct_pct=0.095,
    is_unique=False, ...)

ColumnProfile(name="amount", spark_type="float64", row_count=1000,
    null_count=0, distinct_count=1000, is_unique=True,
    mean_value=~255.0, std_value=~141.0, ...)

ColumnProfile(name="status", spark_type="object", row_count=1000,
    null_count=0, distinct_count=3, distinct_pct=0.003,
    top_values=[{"value": "active", "count": ~334}, ...])

ColumnProfile(name="created_at", spark_type="datetime64[ns]", row_count=1000,
    null_count=0, distinct_count=1000, is_unique=True, ...)
```

**5. Step 2: semantic_typer** → string columns only

`status` has `spark_type="object"`, so it gets typed:
```
infer_semantic_type(["active", "inactive", "pending"], "status")
→ Inference(value=SemanticType.ENUM, confidence=0.95,
    evidence=["3 distinct values", "all lowercase single words"],
    method="pattern_matching")
```

**6. Step 3: role_classifier** → all columns

```
infer_column_role(order_id_profile)
→ Inference(value=ColumnRole.PRIMARY_KEY, confidence=0.92,
    evidence=["unique=True", "name contains 'id'", "sequential integers"],
    runner_ups=[CompetingHypothesis(value=ColumnRole.SURROGATE_KEY, confidence=0.70)])

infer_column_role(amount_profile)
→ Inference(value=ColumnRole.MEASURE, confidence=0.88,
    evidence=["float type", "high cardinality", "name suggests monetary"])

infer_column_role(created_at_profile)
→ Inference(value=ColumnRole.TIMESTAMP, confidence=0.95,
    evidence=["datetime type", "name contains 'created'", "monotonically increasing"])
```

**7. Step 4: grain_detector** → `detect_grain(df, profiles)`

Strategy: test unique columns first (order_id is unique → immediate hit).
```python
GrainAnalysis(
    best_grain=["order_id"],
    is_unique=True,
    duplicate_rate=0.0,
    candidates_tested=[
        {"columns": ["order_id"], "distinct_count": 1000, "dup_rate": 0.0},
    ],
    inference=Inference(value="order_id", confidence=0.95,
        evidence=["single column unique", "role=primary_key"])
)
```

**8. Step 5: freshness** → `detect_freshness(df, profiles)`

Finds `created_at` (role=TIMESTAMP), computes:
```
FreshnessAnalysis(
    freshness_column="created_at",
    latest_value="2026-02-11 15:00:00",
    earliest_value="2026-01-01 00:00:00",
    staleness="fresh",
    staleness_hours=0.5,
    cadence="hourly",
    avg_rows_per_period=1.0
)
```

**9. Step 6: table_classifier** → `classify_table(profiles, grain, freshness)`

Uses evidence from profiles + grain + freshness:
```
Inference(
    value=TableClassification.FACT,
    confidence=0.82,
    evidence=["has measures (amount)", "has timestamp", "unique grain",
              "high row count relative to dimension"],
    runner_ups=[CompetingHypothesis(value=TableClassification.EVENT_LOG,
        confidence=0.65, evidence=["hourly cadence", "timestamp-ordered"])]
)
```

**10. Steps 7–13**: Format checks, cleanliness, duplicate forensics (trivial here —
no duplicates given unique grain), string length, outlier detection on `amount`,
cross-column (no co-null patterns with no nulls), value stability.

**11. Assembly**

All results are assembled into a `TableProfile`. Quality score computed by
deducting penalties:
- `-0.05 × affected_pct` per ERROR-severity issue
- `-0.02 × affected_pct` per WARNING-severity issue
- `-0.10` if **average** column null rate > 20%
- `-0.05` if grain exists but is **not unique** (duplicates detected)

`overall_quality_score` maps to `quality_summary` as: ≥ 0.90 → `"excellent"`, ≥ 0.75 → `"good"`, ≥ 0.50 → `"fair"`, < 0.50 → `"poor"`.

Final: `overall_quality_score = 1.0` (no issues in this clean dataset).

---

## Worked Example 2: grain_detector — Multi-Column Grain

Input: A DataFrame with composite key (no single unique column).

```python
df = pd.DataFrame({
    "store_id": [1, 1, 1, 2, 2, 2, 3, 3, 3],
    "product_id": ["A", "B", "C", "A", "B", "C", "A", "B", "C"],
    "sales": [100, 200, 150, 300, 250, 175, 50, 400, 225],
})
```

### Execution trace:

**1. Compute column stats** (or reuse provided profiles)
```
store_id:   distinct=3, unique=False, null_pct=0.0
product_id: distinct=3, unique=False, null_pct=0.0
sales:      distinct=9, unique=True, null_pct=0.0
```

**2. Identify candidate key columns**

Filter: non-constant, non-null, not flagged as measure.
- `store_id`: distinct_pct = 0.33 (candidate)
- `product_id`: distinct_pct = 0.33 (candidate)
- `sales`: distinct_pct = 1.0, but type=numeric → de-prioritized (measures rarely keys)

**3. Test single columns**
```
["store_id"]:   distinct_count=3/9 → dup_rate=0.67 (not unique)
["product_id"]: distinct_count=3/9 → dup_rate=0.67 (not unique)
["sales"]:      distinct_count=9/9 → dup_rate=0.0 (unique, but deprioritized)
```

**4. Test 2-column combinations** (since no strong single-column key)
```
["store_id", "product_id"]: distinct_count=9/9 → dup_rate=0.0 ✔️
```

**5. Result**
```python
GrainAnalysis(
    best_grain=["store_id", "product_id"],
    is_unique=True,
    duplicate_rate=0.0,
    candidates_tested=[
        {"columns": ["store_id"], "distinct_count": 3, "dup_rate": 0.67},
        {"columns": ["product_id"], "distinct_count": 3, "dup_rate": 0.67},
        {"columns": ["store_id", "product_id"], "distinct_count": 9, "dup_rate": 0.0},
    ],
    runner_up_grains=[
        {"columns": ["sales"], "distinct_count": 9, "dup_rate": 0.0,
         "note": "deprioritized: numeric measure column"}
    ],
    inference=Inference(
        value=["store_id", "product_id"],
        confidence=0.90,
        evidence=["2-col combo unique", "both non-null", "both categorical"],
        counter_signals=["sales is also unique (but is a measure)"],
        method="exhaustive_combination"
    )
)
```

**6. Natural follow-up: use `best_grain` directly with `case_file`**

The `best_grain` result is designed to feed directly into `case_file`. Since
`["store_id", "product_id"]` is a composite key, pass it to `key_columns` and use
tuples in `row_ids` — one tuple per row:

```python
# Investigate specific rows by composite key
case_file(
    df,
    key_columns=grain.best_grain,          # ["store_id", "product_id"]
    row_ids=[(1, "A"), (2, "B")],          # tuples, positionally aligned
)

# Or investigate duplicates if grain were not unique:
case_file(df, key_columns=grain.best_grain, filter="duplicates")
```

See Worked Example 4 for the full `case_file` + `row_ids` walkthrough.

---

## Worked Example 3: microscope — Numeric Column with Outliers

Input: An `amount` column with a few extreme values.

```python
df = pd.DataFrame({
    "order_id": range(1, 101),
    "amount": [50.0] * 90 + [5000.0, 6000.0, 7000.0, -100.0, -200.0,
               0.01, 0.02, 0.03, 100000.0, 999999.0],
})
```

### Execution trace:

**1. Engine detection + pandas conversion**
```
engine = "pandas", no conversion needed
Sampling: 100 rows (under _MAX_SAMPLE_FOR_DISTRIBUTION threshold)
```

**2. Classify column type**
```
series.dtype = float64 → col_type = "numeric"
```

**3. Common analysis** (`_analyze_common`)
```
metrics:
    row_count: 100
    null_count: 0
    null_pct: 0.0
    distinct_count: 13
    distinct_pct: 0.13
    is_unique: False
```

**4. Numeric analysis** (`_analyze_numeric`)
```
metrics:
    mean: ~11,100.01
    median: 50.0
    std: ~100,205.3
    min: -200.0
    max: 999,999.0
    q1: 50.0
    q3: 50.0
    iqr: 0.0  (90% of values are 50.0)
    skewness: ~9.8 (heavily right-skewed)

Histogram (bin_count=20):
    bin [-200, 49750]: 95 rows
    bin [49750, 99500]: 0 rows
    ...
    bin [949750, 999500]: 0 rows
    bin [999500, 999999]: 1 row
```

**5. Outlier detection** (IQR method)

Since IQR=0 (constant-dominated), falls back to percentile method:
```
lower_bound: p5 = 0.02
upper_bound: p95 = 5000.0

Outliers below lower_bound: -200.0, -100.0 (2 rows)
Outliers above upper_bound: 6000.0, 7000.0, 100000.0, 999999.0 (4 rows)

Total outliers: 6 (6% of rows)
```

**6. Assemble output**

```python
{
    "kind": "microscope",
    "subject": "amount",
    "summary": "amount: 100 rows, 0 nulls, 13 distinct | median=50.0, heavily right-skewed | 6 outliers (6%)",
    "metrics": {
        "row_count": 100,
        "null_count": 0,
        "distinct_count": 13,
        "mean": 11100.01,
        "median": 50.0,
        "std": 100205.3,
        "skewness": 9.8,
        "outlier_count": 6,
        "outlier_pct": 0.06,
    },
    "findings": [
        "Constant-dominated: 90% of values are 50.0",
        "Heavily right-skewed (skewness=9.8)",
        "Negative values present (min=-200.0)",
        "Range spans 6 orders of magnitude",
    ],
    "risks": [
        "6 outliers (6%) — may distort mean-based aggregations",
        "Negative values may indicate refunds or data errors",
    ],
    "samples": {
        "top_values": [{"value": 50.0, "count": 90, "pct": 0.9}],
        "outlier_values": [
            {"value": 999999.0, "row_idx": 99},
            {"value": 100000.0, "row_idx": 98},
            {"value": -200.0, "row_idx": 94},
        ],
    },
    "suggested_next_actions": [
        "case_file(df, column='amount', filter='outliers') — see full row context",
        "case_file(df, column='amount', filter='where:amount < 0') — investigate negatives",
    ]
}
```

---

## Worked Example 4: case_file — Row ID Lookup

Input: A fact table with a composite key `(store_id, product_id)`, where some rows
have suspicious `sales` values identified from a prior `microscope` run.

```python
df = pd.DataFrame({
    "store_id":   [1, 1, 1, 2, 2, 2, 3, 3, 3],
    "product_id": ["A", "B", "C", "A", "B", "C", "A", "B", "C"],
    "sales":      [100, 200, -9999, 300, 250, 175, 50, 400, 0],
    "region":     ["East"] * 3 + ["West"] * 3 + ["East"] * 3,
})
```

### Scenario A: Single key column — flat list of scalars

When one column uniquely identifies a row, pass `row_ids` as a plain list:

```python
case_file(df, key_columns=["store_id"], row_ids=[1, 3])
```

Internally: `df["store_id"].isin([1, 3])` — matches all rows where `store_id` is 1 or 3 (6 rows).

**Output:**
```python
{
    "kind": "case_file",
    "summary": "6 rows with row_ids on store_id (66.7% of 9)",
    "metrics": {"matched_rows": 6, "total_rows": 9, "matched_pct": 0.6667},
    "samples": {
        "rows": [
            {"store_id": 1, "product_id": "A", "sales": 100, "region": "East"},
            {"store_id": 1, "product_id": "B", "sales": 200, "region": "East"},
            {"store_id": 1, "product_id": "C", "sales": -9999, "region": "East"},
            ...
        ]
    }
}
```

### Scenario B: Composite key — list of tuples

When the key is composite, each `row_ids` entry must be a **tuple** whose values
align positionally with `key_columns`:

```python
case_file(
    df,
    key_columns=["store_id", "product_id"],
    row_ids=[(1, "C"), (2, "A"), (3, "C")],   # tuple per row: (store_id, product_id)
)
```

Internally, for each tuple `(sv, pv)`:
```python
mask = (df["store_id"] == sv) & (df["product_id"] == pv)
```

Each tuple is AND'd together — `(1, "C")` matches exactly the row where
`store_id=1 AND product_id="C"`.

**Output:**
```python
{
    "kind": "case_file",
    "summary": "3 rows with row_ids (33.3% of 9)",
    "metrics": {"matched_rows": 3, "total_rows": 9},
    "samples": {
        "rows": [
            {"store_id": 1, "product_id": "C", "sales": -9999, "region": "East"},
            {"store_id": 2, "product_id": "A", "sales": 300,   "region": "West"},
            {"store_id": 3, "product_id": "C", "sales": 0,     "region": "East"},
        ]
    }
}
```

### ⚠️ Silent-skip gotcha

With multiple `key_columns`, any `row_ids` entry that is NOT a `list` or `tuple` is
**silently skipped** — no error, no match:

```python
# ❌ Wrong — passing scalars when key is composite
case_file(df, key_columns=["store_id", "product_id"], row_ids=[1, 2, 3])
# → 0 rows matched (scalars silently skipped, not raised as errors)

# ✅ Correct — tuples
case_file(df, key_columns=["store_id", "product_id"], row_ids=[(1, "C"), (2, "A")])
```

If your lookup returns fewer rows than expected, the first thing to check is whether
you passed scalars instead of tuples.

### Scenario C: Filter mode — investigate suspicious values

`row_ids` is optional. When you don't know which rows to look at,
use `filter` with `column` to let case_file select them:

```python
# Find rows where sales is negative
case_file(df, column="sales", filter="where:sales < 0")

# Find rows with the most common sales values
case_file(df, column="sales", filter="top:3")

# Find all duplicate rows if grain is composite
case_file(df, key_columns=["store_id", "product_id"], filter="duplicates")
```

### Scenario D: Using `profile.grain.best_grain` as key

The natural end-to-end flow connects `profile_table` directly to `case_file`:

```python
profile = profile_table(df, subject="catalog.schema.sales")
grain = profile.grain.best_grain        # e.g. ["store_id", "product_id"]

# Investigate specific rows from profiler findings
case_file(
    df,
    key_columns=grain,
    row_ids=[(1, "C"), (3, "C")],       # tuples because grain is composite
    profile=profile,                    # pass profile for richer co-occurrence context
)
```

Passing `profile=` lets `case_file` cross-reference its findings against the full
table profile (e.g. richer co-occurrence context, grain-aware duplicate detection).

---

## Key Python Patterns

### Inference Dataclass with Confidence/Evidence/Runner-Ups

```python
# Note: use relative import within the package
from .models import Inference, CompetingHypothesis

# Every classification produces an Inference wrapping the result:
result = Inference(
    value=ColumnRole.PRIMARY_KEY,
    confidence=0.92,
    evidence=["unique=True", "name contains 'id'"],
    counter_signals=["no auto-increment pattern"],
    sample_size=1000,
    method="heuristic_rules",
    runner_ups=[
        CompetingHypothesis(
            value=ColumnRole.NATURAL_KEY,
            confidence=0.70,
            evidence=["unique but not sequential"],
            blocker_reason="sequential pattern detected",
            verification_hint="Check if values are system-generated"
        )
    ]
)
```

This pattern ensures every profiler decision is:
- **Auditable**: confidence + evidence explain the decision
- **Recoverable**: runner_ups let downstream tools consider alternatives
- **Actionable**: verification_hint guides human validation

### Enum-Based Classification

```python
class TableClassification(str, Enum):
    FACT = "fact"
    DIMENSION = "dimension"
    # ...

# str inheritance means enum values serialize naturally to JSON:
json.dumps({"type": TableClassification.FACT})  # {"type": "fact"}
```

### Extension Registry (Plugin Support)

```python
# extension_registry.py provides:
def register_extension(name: str, fn: Callable) -> None: ...
def run_extensions(df, profiles, context) -> list[dict]: ...

# Custom analysis steps plug in without touching profiler.py:
register_extension("my_custom_check", my_check_function)
```

### Dual-Engine DataFrame Handling (_sampling.py)

```python
def detect_engine(df) -> str:
    """Return 'spark' or 'pandas' based on DataFrame type."""
    if type(df).__module__.startswith("pyspark"):
        return "spark"
    return "pandas"

# All sub-modules call detect_engine() and branch:
if engine == "spark":
    count = df.select(F.countDistinct(col)).collect()[0][0]
else:
    count = pdf[col].nunique()
```

The early materialization optimization in `profiler.py` means most tables end up
on the pandas path regardless of their original engine, but each sub-module
still handles both paths for correctness on large tables that exceed the threshold.

### Graceful Degradation

```python
# Every step in the pipeline is wrapped:
try:
    grain = detect_grain(df, profiles)
except Exception as exc:
    degraded_features.append("grain")
    degradation_reasons["grain"] = str(exc)
```

If any step fails, the profile still completes with partial results. The
`degraded_features` list and `degradation_reasons` dict make failures visible
without crashing the entire profiling run.

### where: Filter Expression Patterns

The `where:` filter passes the expression directly to `df.query()`, which uses the
Python engine and supports any pandas Series method call on column names:

```python
# Null / not-null checks (equivalent to filter="nulls")
case_file(df, column="status",      filter="where:status.isnull()")
case_file(df, column="customer_id", filter="where:customer_id.notna()")

# String methods — Python engine activates automatically
case_file(df, column="code",        filter="where:code.str.startswith('ERR')")
case_file(df, column="name",        filter="where:name.str.contains('duplicate', case=False)")
case_file(df, column="notes",       filter="where:notes.str.len() > 500")
case_file(df, column="email",       filter="where:email.str.endswith('.test')")

# Datetime accessors
case_file(df, column="created_at",  filter="where:created_at.dt.year == 2023")
case_file(df, column="load_ts",     filter="where:load_ts.dt.hour < 6")    # off-hours loads
case_file(df, column="event_ts",    filter="where:event_ts.dt.dayofweek == 6")  # Sundays

# Numeric range and membership
case_file(df, column="amount",      filter="where:amount.between(0, 0.01)")
case_file(df, column="category",    filter="where:category in ['UNKNOWN', 'MISC', 'OTHER']")

# Compound conditions
case_file(df, column="status",      filter="where:status == 'active' and region != 'East'")
case_file(df, column="amount",      filter="where:amount < 0 and status == 'paid'")
```

Named filters (`nulls`, `outliers`, `pattern:XXX`, `duplicates`, `format_issues`) require
dynamic computation that is not expressible as a static filter string. Use them in
preference to `where:` when they cover the use case — they are faster and produce richer
co-occurrence context.

> **`@variable` gotcha**: `df.query()` supports `@varname` for local Python variables,
> but this does NOT work through `where:` — the expression is a string with no access
> to your calling scope. Embed thresholds as literals:
> `"where:amount > 1000"` not `"where:amount > @threshold"`.
