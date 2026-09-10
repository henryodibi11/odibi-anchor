# table_contract_summary — Code Walkthrough

**Module:** `table_contract_summary.py` (1,785 lines)  
**Purpose:** Compact "table card" — key detection, freshness, column roles, value examples.

---

## How It Works

`table_contract_summary` produces a compact "table card" that summarizes the most important characteristics of a DataFrame without requiring deep analysis. It answers: What are the keys? How fresh is the data? What role does each column play? What values are typical?

Key features:
- **Automatic key detection** — identifies likely primary keys by null rate and uniqueness
- **Column role classification** — categorizes columns as identifier, categorical, timestamp, numeric, or text
- **Freshness assessment** — detects temporal columns and reports data staleness
- **Value examples** — provides representative values for each column

---

## Worked Example 1: Key and Role Detection

### Input

```
df (1000 rows):
  order_id (int64, unique, 0% null)
  status (object, 5 distinct values: ["pending", "shipped", "delivered", "cancelled", "returned"])
  amount (float64, range 10.0–10000.0)
  updated_at (datetime64, max = 2026-06-08)
```

### Step-by-Step Execution

**Step 1: Profile each column**

Compute per-column statistics:

| Column | null_rate | unique_rate | dtype | n_distinct |
|---|---|---|---|---|
| order_id | 0.0 | 1.0 | int64 | 1000 |
| status | 0.0 | 0.005 | object | 5 |
| amount | 0.02 | 0.95 | float64 | 950 |
| updated_at | 0.0 | 0.8 | datetime64 | 800 |

**Step 2: Key detection**

For each column, check key candidacy thresholds:

```python
_KEY_MAX_NULL_RATE = 0.01    # keys must have <1% nulls
_KEY_MIN_UNIQUE_RATE = 0.98  # keys must be >98% unique
```

- `order_id`: null_rate=0.0 < 0.01 ✓, unique_rate=1.0 >= 0.98 ✓ → **candidate key**
- `status`: unique_rate=0.005 < 0.98 → not a key
- `amount`: unique_rate=0.95 < 0.98 → not a key
- `updated_at`: unique_rate=0.8 < 0.98 → not a key

**Step 3: Role classification**

Using name hints and statistical properties:

| Column | Name matches | Statistical signal | Assigned role |
|---|---|---|---|
| order_id | `_IDENTIFIER_NAME_HINTS` ("id") | high uniqueness | identifier |
| status | (none) | low cardinality (5 values) | categorical |
| amount | (none) | high cardinality, numeric | numeric |
| updated_at | `_FRESHNESS_NAME_HINTS` ("updated") | datetime type | timestamp |

Hint sets used:
- `_IDENTIFIER_NAME_HINTS`: {"id", "key", "pk", "uuid", "guid", "code"}
- `_FRESHNESS_NAME_HINTS`: {"updated", "modified", "created", "timestamp", "date"}
- `_FLAG_NAME_HINTS`: {"active", "enabled", "flag", "is_", "has_"}

**Step 4: Freshness assessment**

`updated_at` is the freshness column (matches hint + datetime type):
- `max(updated_at)` = 2026-06-08
- `reference_time` = 2026-06-08 (today)
- Days since last update = 0
- `stale_after_days` default = 7
- 0 < 7 → `freshness_status = "fresh"`

### Output

```python
{
    "kind": "contract",
    "subject": "orders_table",
    "metrics": {
        "row_count": 1000,
        "column_count": 4,
        "candidate_keys": ["order_id"],
        "freshness_status": "fresh",
        "freshness_column": "updated_at",
        "days_since_update": 0
    },
    "findings": [
        "Candidate key detected: order_id (unique_rate=1.0, null_rate=0.0)",
        "Data is fresh (last update: 2026-06-08)"
    ],
    "risks": [],
    "column_roles": {
        "order_id": {"role": "identifier", "is_key": True},
        "status": {"role": "categorical", "n_distinct": 5, "examples": ["pending", "shipped", "delivered"]},
        "amount": {"role": "numeric", "min": 10.0, "max": 10000.0},
        "updated_at": {"role": "timestamp", "min": "2026-01-01", "max": "2026-06-08"}
    }
}
```

---

## Worked Example 2: Freshness Check with Staleness

### Input

```
df (500 rows):
  id (int64, unique)
  value (float64)
  last_modified (datetime64, max = 2026-05-28)

stale_after_days=7
reference_time=2026-06-08
```

### Step-by-Step Execution

**Step 1: Identify freshness column**

`last_modified` matches `_FRESHNESS_NAME_HINTS` ("modified") and is datetime type → selected as freshness column.

**Step 2: Compute staleness**

```python
max_value = pd.Timestamp("2026-05-28")
reference = pd.Timestamp("2026-06-08")
days_elapsed = (reference - max_value).days  # = 11
```

**Step 3: Compare to threshold**

```python
days_elapsed = 11
stale_after_days = 7
11 > 7  → freshness_status = "stale"
```

**Step 4: Generate risk**

```python
risks.append(
    f"Data is 11 days stale (threshold: 7 days). "
    f"Last update: 2026-05-28. Consider investigating pipeline freshness."
)
```

### Output

```python
{
    "kind": "contract",
    "metrics": {
        "row_count": 500,
        "freshness_status": "stale",
        "freshness_column": "last_modified",
        "days_since_update": 11,
        "stale_threshold": 7
    },
    "findings": [
        "Candidate key detected: id",
        "Data is STALE: 11 days since last update (threshold: 7 days)"
    ],
    "risks": [
        "Data is 11 days stale (threshold: 7 days). Last update: 2026-05-28."
    ]
}
```

---

## Python Patterns

- **`_FRESHNESS_NAME_HINTS`, `_IDENTIFIER_NAME_HINTS`, `_FLAG_NAME_HINTS` sets** — heuristic role detection using column name substrings; checked via `any(hint in col_name.lower() for hint in hints)`
- **`Sequence[str]` for flexible column list inputs** — accepts lists, tuples, or any sequence for column specification parameters
- **`reference_time` parameter for deterministic staleness** — enables testable freshness checks without depending on `datetime.now()`; tests can inject a fixed reference time
- **`spark_sample_size` for bounded Spark profiling** — limits the number of rows scanned on large Spark DataFrames to keep profiling fast while still producing meaningful statistics
- **Dual key detection signals** — combines statistical evidence (uniqueness rate) with name-based heuristics ("id", "key" in name) for robust key identification
- **Tiered freshness output** — `"fresh"` / `"stale"` / `"unknown"` with configurable thresholds and fallback to Delta metadata when no temporal columns exist
