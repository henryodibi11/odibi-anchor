# exploration_context — Code Walkthrough

One-call first-look that combines dataset profiling, grain analysis, freshness
detection, and key suggestions. (Note: `anchor("explore")` has been removed; use `anchor("profile_table")` which provides all these capabilities and more.)

**Source:** `src/odibi_anchor/profiling/exploration_context.py` (1,048 lines)

---

## Module Overview

### Composite Context Builder

Unlike `dataset_profile_context` (which focuses purely on column-level profiling),
`exploration_context` orchestrates three analyses in a single call:

1. **Column profiling** — quick null/distinct/stats (lighter than full `dataset_profile_context`)
2. **Grain analysis** — automatically detect the table’s primary key or composite grain
3. **Freshness detection** — find the most recent temporal column and compute staleness

This eliminates the 3-5 call sequence agents typically perform when encountering
an unfamiliar table.

### Candidate Key Generation via Column Name Heuristics

The module uses three `frozenset` hint collections for column classification:

```python
_IDENTIFIER_HINTS = {"id", "key", "code", "number", "num", "pk", "sk"}
_TEMPORAL_HINTS = {"date", "time", "timestamp", "datetime", "created", "updated",
                   "modified", "snapshot", "effective", "loaded", "ingested", "asof"}
_GRAIN_PARTITION_HINTS = {"snapshot_year", "snapshot_month", "snapshot_date", ...,
                          "partition", "period", "batch", "version", "revision",
                          "year", "month", "week", "day", "quarter"}
```

These drive automatic grain candidate generation without requiring the caller to
specify which columns to test.

### Output Shape

```python
{
    "kind": "exploration_context",
    "subject": str,
    "summary": str,
    "metrics": {"row_count", "column_count", "high_null_column_count",
               "unique_columns", "grain_is_unique", "has_freshness_signal"},
    "columns": list[str],
    "column_profiles": dict,           # lighter than dataset_profile_context
    "grain_analysis": {"best_grain", "is_unique", "candidates_tested", "duplicate_rate"},
    "freshness": {"column", "min", "max", "staleness", "staleness_hours"} | None,
    "suggested_keys": list[str],
    "findings": list[str],
    "risks": list[str],
    "samples": dict,
    "suggested_next_actions": list[str],
    "delta_metadata": dict | None,     # Spark-only: DESCRIBE DETAIL + HISTORY
    "parameters": dict,
}
```

---

## Worked Example 1: `_generate_grain_candidates`

### Function Signature

```python
def _generate_grain_candidates(
    columns: list[str],
    column_profiles: dict[str, dict[str, Any]],
) -> list[list[str]]:
```

### Scenario

A DataFrame with columns: `["order_id", "customer_name", "order_date", "amount", "region"]`.
No single column is already known to be unique.

### Step-by-Step Trace

```
Input:
  columns = ["order_id", "customer_name", "order_date", "amount", "region"]
  column_profiles = {
      "order_id":      {"is_unique": False, ...},
      "customer_name": {"is_unique": False, ...},
      "order_date":    {"is_unique": False, ...},
      "amount":        {"is_unique": False, ...},
      "region":        {"is_unique": False, ...},
  }

Step 1: Find single unique columns
  unique_cols = [c for c, p in column_profiles.items() if p.get("is_unique")]
  → []  (none are unique)
  → No single-column combos added

Step 2: Find ID-named columns
  Check each column against _IDENTIFIER_HINTS = {"id", "key", "code", "number", "num", "pk", "sk"}
  "order_id" → "id" is in _IDENTIFIER_HINTS? Yes (substring match via `any(hint in c.lower())`)
  "customer_name" → no hint match
  "order_date" → no hint match ("date" is in _TEMPORAL_HINTS, not _IDENTIFIER_HINTS)
  "amount" → no match
  "region" → no match
  id_cols = ["order_id"]
  → Add ["order_id"] as single-column candidate

Step 3: Find partition columns
  Check against _GRAIN_PARTITION_HINTS
  None of "order_id", "customer_name", "order_date", "amount", "region" match
  partition_cols = []
  → Skip ID + partition combos

Step 4: Find temporal columns
  Check against _TEMPORAL_HINTS = {"date", "time", "timestamp", ...}
  "order_date" → "date" in _TEMPORAL_HINTS? Yes
  temporal_cols = ["order_date"]

  Generate ID + temporal combos:
  for id_col in id_cols[:3]:   → ["order_id"]
    for t_col in temporal_cols[:4]:  → ["order_date"]
      if id_col != t_col:  → True
        combos.append(["order_id", "order_date"])

Step 5: Deduplicate (using tuple(sorted(combo)) as key)
  seen = set()
  deduped = [["order_id"], ["order_id", "order_date"]]  (both unique tuples)

Step 6: Cap at _MAX_GRAIN_COMBOS_TO_TEST (20)
  2 candidates < 20 → no trimming

Output: [["order_id"], ["order_id", "order_date"]]
```

### Key Insight

The algorithm is priority-ordered: unique single columns > ID-named singles >
ID + partition combos > ID + temporal combos > fallback. It stops generating
more combinations from a category once it has enough candidates (capped at 20 total).

---

## Worked Example 2: `_analyze_grain_pandas`

### Function Signature

```python
def _analyze_grain_pandas(
    df: pd.DataFrame,
    columns: list[str],
    column_profiles: dict[str, dict[str, Any]],
    candidate_keys: list[str] | None,
) -> dict[str, Any]:
```

### Scenario A: Unique grain found on first candidate

A 1000-row DataFrame where `order_id` is truly unique.

```
Input: df with 1000 rows
  candidate_keys = None  (auto-detect)
  Generated combos: [["order_id"], ["order_id", "order_date"]]

Step 1: Test ["order_id"]
  dup_count = int(df.duplicated(subset=["order_id"], keep=False).sum())
  → 0  (no duplicates)
  is_unique = (0 == 0) → True
  dup_rate = 0.0
  candidates_tested.append({"columns": ["order_id"], "is_unique": True, ...})

  Check: is_unique and (not best_grain or len(combo) < len(best_grain))
  → True and (not [] or ...) → set best_grain = ["order_id"]

Step 2: Test ["order_id", "order_date"]
  (continues testing remaining candidates)
  dup_count = 0  → also unique
  But len(["order_id", "order_date"]) = 2 > len(["order_id"]) = 1
  → best_grain stays ["order_id"] (prefer shorter)

Output:
  {"best_grain": ["order_id"], "is_unique": True,
   "candidates_tested": [{"columns": [...], ...}, ...],
   "duplicate_rate": 0.0}
```

### Scenario B: No single column is unique

A 1000-row DataFrame where `order_id` has duplicates (composite key needed).

```
Input: df with 1000 rows
  Generated combos: [["order_id"], ["customer_id"], ["customer_id", "order_date"]]

Step 1: Test ["order_id"]
  dup_count = 100  (50 rows appear twice)
  is_unique = False
  dup_rate = round(100 / 1000, 4) = 0.1

Step 2: Test ["customer_id"]
  dup_count = 1600  (many repeats)
  is_unique = False
  dup_rate = 1.6  (but actually max 1.0 since it's proportion)
  Note: dup_rate uses duplicated(keep=False) which counts ALL copies

Step 3: Test ["customer_id", "order_date"]
  dup_count = 0  → unique!
  is_unique = True
  best_grain = ["customer_id", "order_date"]

Output:
  {"best_grain": ["customer_id", "order_date"], "is_unique": True,
   "candidates_tested": [3 entries], "duplicate_rate": 0.0}
```

### Scenario C: No unique combination found (fallback)

```
When no candidate achieves is_unique=True:
  best_candidate = min(candidates_tested, key=lambda x: x["duplicate_rate"])
  best_grain = best_candidate["columns"]
  → Returns the least-duplicated combination as "best guess"
  is_unique remains False
```

---

## Worked Example 3: `_detect_freshness_pandas`

### Function Signature

```python
def _detect_freshness_pandas(
    df: pd.DataFrame,
    columns: list[str],
    column_profiles: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
```

### Scenario

A DataFrame with a column `updated_at` (datetime64) whose max value is
19.5 hours before current time.

### Step-by-Step Trace

```
Input: df with columns ["id", "name", "amount", "updated_at", "created_at"]
  df["updated_at"].dtype = datetime64[ns]
  df["created_at"].dtype = datetime64[ns]

Step 1: Find temporal candidates
  for col in columns:
    "id" → is_datetime64_any_dtype? No. Hint match? No.
    "name" → No.
    "amount" → No.
    "updated_at" → is_datetime64_any_dtype? Yes!
      temporal_candidates.append("updated_at")
    "created_at" → is_datetime64_any_dtype? Yes!
      temporal_candidates.append("created_at")
  temporal_candidates = ["updated_at", "created_at"]

Step 2: Priority selection
  priority_hints = {"updated", "modified", "loaded", "ingested", "snapshot"}
  Check "updated_at": "updated" in "updated_at".lower()? Yes!
  best_col = "updated_at"  (selected over "created_at" due to priority)

Step 3: Compute freshness
  series = pd.to_datetime(df["updated_at"], errors="coerce").dropna()
  max_val = series.max()  → 2026-06-07 14:30:00
  now = pd.Timestamp.now()  → 2026-06-08 10:00:00
  staleness = now - max_val  → Timedelta('0 days 19:30:00')
  hours = 19.5

Step 4: Format staleness string
  hours < 1? No
  hours < 24? Yes
  staleness_str = f"{19.5:.1f}h ago"  → "19.5h ago"

Output:
  {"column": "updated_at",
   "min": "2026-01-15 08:00:00",
   "max": "2026-06-07 14:30:00",
   "staleness": "19.5h ago",
   "staleness_hours": 19.5}
```

### Alternate Path: No Datetime Columns (Delta Fallback)

When `_detect_freshness_pandas` returns `None` (no temporal columns found),
the Spark implementation has a fallback path:

```python
if freshness is None and delta_metadata and delta_metadata.get("last_modified"):
    # Use Delta log's last_modified timestamp as freshness signal
    freshness = {
        "column": "(delta_log)",
        "min": lm, "max": lm,
        "staleness": staleness_str,
        "staleness_hours": round(hours_ago, 1),
        "source": "delta_metadata",
    }
```

This zero-data-scan approach provides freshness even when the table has no
recognizable temporal columns.

---

## Python Patterns

### 1. `frozenset` for Hint Collections

```python
_IDENTIFIER_HINTS = {"id", "key", "code", "number", "num", "pk", "sk"}
_TEMPORAL_HINTS = {"date", "time", "timestamp", ...}
_GRAIN_PARTITION_HINTS = {"snapshot_year", "snapshot_month", ...}
```

**Why:** O(1) substring lookup via `any(hint in col.lower() for hint in _IDENTIFIER_HINTS)`.
Defined at module level as implicit frozensets (set literals). The hint-based approach
is heuristic but covers 90%+ of real table naming conventions across enterprise data.

### 2. `itertools.combinations` Style Candidate Generation

While the actual code uses explicit loops rather than `itertools.combinations`,
it follows the combinatorial pattern:

```python
# ID + partition combos
for id_col in id_cols[:3]:
    combo = [id_col] + partition_cols
    if len(combo) <= _MAX_GRAIN_COMBO_COLUMNS:
        combos.append(combo)
    for p_col in partition_cols[:4]:
        combos.append([id_col, p_col])

# ID + temporal combos
for id_col in id_cols[:3]:
    for t_col in temporal_cols[:4]:
        if id_col != t_col:
            combos.append([id_col, t_col])
```

**Why:** Explicit loops with slice caps (`[:3]`, `[:4]`) prevent combinatorial
explosion. With 10 ID columns and 10 temporal columns, uncapped combinations
would yield 100 pairs. The slicing keeps candidates under `_MAX_GRAIN_COMBOS_TO_TEST = 20`.

### 3. Early Termination on First Unique Grain

The grain analysis tests all candidates but selects the *shortest* unique one:

```python
if is_unique and (not best_grain or len(combo) < len(best_grain)):
    best_grain = combo
    best_is_unique = True
```

**Why:** A single-column key (`["order_id"]`) is always preferred over a composite
key (`["order_id", "order_date"]`) because it’s simpler for downstream joins and
dedup operations. The algorithm continues testing after finding the first unique
grain to ensure it finds the shortest one.

### 4. `_fetch_delta_metadata` — Optional Spark Integration

```python
def _fetch_delta_metadata(subject: str) -> dict[str, Any] | None:
    # Returns None if:
    # - subject doesn't look like a table name (no dots)
    # - spark is unavailable (catches ImportError)
    # - DESCRIBE DETAIL/HISTORY fails
```

**Why:** The exploration context works on both pandas DataFrames (where no Delta
metadata exists) and Spark tables. By catching `ImportError` and wrapping all
Spark calls in `try/except`, the module degrades gracefully. Non-Spark environments
simply get `delta_metadata: None` in the output.

---

## Key Design Decisions

| Decision | Rationale |
| --- | --- |
| Lighter column profiles than `dataset_profile_context` | Exploration trades depth for speed — no type inference, no null-like detection |
| Grain candidates capped at 20 | Prevents O(n²) DataFrame scans on wide tables |
| Priority hints for freshness column | "updated_at" > "created_at" > first datetime — matches real-world patterns |
| Deduplication via `tuple(sorted(combo))` | Prevents testing ["a", "b"] and ["b", "a"] as separate candidates |
| `_MAX_GRAIN_COMBO_COLUMNS = 4` | Grains wider than 4 columns are rare; testing them is expensive |
| Delta metadata fallback for freshness | Zero-scan alternative when no temporal columns exist |
| `approx_count_distinct` in Spark path | Much faster than exact distinct for large tables; 2% error is acceptable for exploration |