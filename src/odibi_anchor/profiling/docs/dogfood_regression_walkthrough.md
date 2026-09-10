# dogfood_regression_context — Code Walkthrough

Stores baselines of tool output, diffs subsequent runs against them, and
classifies changes as improvements, regressions, or neutral. No DataFrame
dependency — operates entirely on profile dicts and JSON file I/O.

**Source:** `src/odibi_anchor/profiling/dogfood_regression_context.py` (457 lines)

---

## Module Overview

### No DataFrame Dependency

Unlike the other profiling modules, `dogfood_regression_context` never touches
a DataFrame. It operates on the **output dicts** of other context builders:

```python
# Input is a context dict from any tool
current = exploration_context(df, subject="catalog.schema.table")
ctx = dogfood_regression_context(current, save_as_baseline=True)
```

### JSON File I/O for Baselines

Baselines are stored as JSON files in a `.dogfood_baselines/` directory:

```
.dogfood_baselines/
  exploration_context__catalog_schema_table.json
  dataset_profile_context__bronze_invoices.json
```

Each baseline file contains:
```json
{
  "saved_at": "2026-06-08T10:00:00+00:00",
  "saved_at_epoch": 1780966000.0,
  "output": { ... the full context dict ... }
}
```

### Directional Classification

The module uses a `_METRIC_DIRECTIONS` dict to classify changes without
per-field logic:

```python
_METRIC_DIRECTIONS = {
    "completeness_pct": "higher",   # more complete = better
    "uniqueness_pct": "higher",
    "null_pct": "lower",            # fewer nulls = better
    "duplicate_pct": "lower",
    "violation_count": "lower",
    "regressions_count": "lower",
    "risk_count": "lower",
    ...
}
```

For fields with known direction, the algorithm computes:
- Current moved in preferred direction → **improvement**
- Current moved against preferred direction → **regression**
- Same value → **unchanged**

---

## Worked Example 1: `_classify_change`

### Function Signature

```python
def _classify_change(field: str, baseline_val: Any, current_val: Any) -> str:
```

### Scenario A: Known Metric with Direction (Numeric)

```
Input: field="metrics.risk_count", baseline_val=5, current_val=3

Step 1: Extract field key
  field_key = field.split(".")[-1]  → "risk_count"

Step 2: Look up direction
  direction = _METRIC_DIRECTIONS.get("risk_count")  → "lower"

Step 3: Both values are numeric?
  isinstance(5, (int, float)) and isinstance(3, (int, float))  → True

Step 4: Apply direction logic
  direction == "lower":
    current (3) < baseline (5)?  → Yes
    → Return "improvement"

Output: "improvement"
```

### Scenario B: Higher-is-better, Value Decreases

```
Input: field="metrics.completeness_pct", baseline_val=0.95, current_val=0.88

Step 1: field_key = "completeness_pct"
Step 2: direction = "higher"
Step 3: Both numeric? Yes
Step 4: direction == "higher":
  current (0.88) > baseline (0.95)?  → No
  current (0.88) < baseline (0.95)?  → Yes
  → Return "regression"

Output: "regression"
```

### Scenario C: Unknown Field (No Direction)

```
Input: field="metrics.row_count", baseline_val=1000, current_val=1200

Step 1: field_key = "row_count"
Step 2: direction = _METRIC_DIRECTIONS.get("row_count")  → None
Step 3: direction is None → skip numeric comparison
Step 4: Check "risk" in field.lower()?  → No

→ Return "unchanged" (default for unknown fields)

Output: "unchanged"
```

### Scenario D: Risk List (Special Case)

```
Input: field="risks", baseline_val=["risk1", "risk2", "risk3"], current_val=["risk1"]

Step 1: field_key = "risks"
Step 2: direction = None ("risks" not in _METRIC_DIRECTIONS)
Step 3: Skip numeric comparison (not both numeric)
Step 4: "risk" in "risks".lower()?  → Yes!
  isinstance(baseline_val, list) and isinstance(current_val, list)?  → Yes
  len(current_val) (1) < len(baseline_val) (3)?  → Yes
  → Return "improvement"

Output: "improvement"
```

---

## Worked Example 2: `_classify_list_change`

### Function Signature

```python
def _classify_list_change(field: str, baseline_val: list, current_val: list) -> str:
```

### Scenario A: Risks Reduced

```
Input: field="risks",
       baseline=["null risk", "dup risk", "skew risk"],
       current=["null risk"]

Step 1: Check "risk" in field.lower()
  "risk" in "risks"?  → Yes

Step 2: Compare lengths
  len(current) = 1, len(baseline) = 3
  1 < 3?  → Yes
  → Return "improvement"  (fewer risks = better)

Output: "improvement"
```

### Scenario B: Findings Increased

```
Input: field="findings",
       baseline=["shape: 100 rows"],
       current=["shape: 100 rows", "potential key: id", "high-null: col_x"]

Step 1: Check "risk" in "findings".lower()  → No

Step 2: Check "finding" in "findings".lower()  → Yes
  len(current) (3) > len(baseline) (1)?  → Yes
  → Return "improvement"  (more findings = more detail = better)

Output: "improvement"
```

### Scenario C: Neither Risk nor Finding

```
Input: field="metrics.some_list",
       baseline=["a", "b"],
       current=["a", "c"]

Step 1: "risk" in field?  → No
Step 2: "finding" in field?  → No
→ Return "unchanged"  (unknown list field, can't determine direction)

Output: "unchanged"
```

---

## Worked Example 3: Full Pipeline End-to-End

### Scenario

A tool’s output is compared against a stored baseline. The baseline was saved
4 hours ago. Two metrics improved (risk_count down, findings up) and none regressed.

### Step-by-Step Trace

```
Input:
  current_output = {
      "kind": "exploration_context",
      "subject": "catalog.schema.orders",
      "metrics": {"row_count": 1000, "risk_count": 2, "high_null_column_count": 1},
      "grain_analysis": {"best_grain": ["order_id"], "is_unique": True},
      "freshness": {"column": "updated_at", "staleness_hours": 2.5},
      "risks": [{"message": "1 high-null column"}],
      "findings": ["Shape: 1000 rows", "Grain: [order_id]", "Freshness: 2.5h"]
  }
  save_as_baseline = False
  Baseline file exists at .dogfood_baselines/exploration_context__catalog_schema_orders.json

═══ Phase 1: Setup & Load ═══

Step 1: Validate inputs
  isinstance(current_output, dict)? Yes
  "kind" in current_output? Yes

Step 2: Resolve paths
  root_path = Path.cwd().resolve()
  slug = _slugify("exploration_context__catalog.schema.orders")
    → re.sub(r"[^a-zA-Z0-9_]", "_", ...) → "exploration_context__catalog_schema_orders"
    → re.sub(r"_+", "_", ...) → "exploration_context__catalog_schema_orders"
    → .strip("_").lower() → "exploration_context__catalog_schema_orders"
  baseline_file = root_path / ".dogfood_baselines" / "exploration_context__catalog_schema_orders.json"

Step 3: Load baseline
  _load_baseline(baseline_file):
    path.exists()? Yes
    json.load(f) → {
        "saved_at_epoch": 1780951600.0,  (4 hours ago)
        "output": {
            "metrics": {"row_count": 950, "risk_count": 4, "high_null_column_count": 2},
            "risks": [{...}, {...}, {...}, {...}],
            "findings": ["Shape: 950 rows"],
            ...
        }
    }

═══ Phase 2: Compute Diffs ═══

Step 4: _baseline_age_hours(baseline)
  saved_epoch = 1780951600.0
  age = (time.time() - 1780951600.0) / 3600.0  → 4.0

Step 5: _compute_diffs(baseline["output"], current_output)
  Iterate _COMPARE_FIELDS = ["metrics", "grain_analysis", "freshness", "risks", "findings"]

  "metrics" (both dicts):
    Compare each key:
      "row_count": 950 vs 1000 → _classify_change("metrics.row_count", 950, 1000)
        field_key = "row_count", direction = None → "unchanged"
      "risk_count": 4 vs 2 → _classify_change("metrics.risk_count", 4, 2)
        direction = "lower", current (2) < baseline (4) → "improvement"
      "high_null_column_count": 2 vs 1 → _classify_change(...)
        direction = None → "unchanged"

  "grain_analysis" (both dicts):
    "best_grain": ["order_id"] vs ["order_id"] → same, skip
    "is_unique": True vs True → same, skip

  "freshness" (both dicts):
    "staleness_hours": compare if changed → different value but no direction → "unchanged"

  "risks" (both lists):
    baseline_val = [{...}, {...}, {...}, {...}] (4 items)
    current_val = [{"message": "1 high-null column"}] (1 item)
    Different! → _classify_list_change("risks", ..., ...)
      "risk" in "risks" → Yes, len(1) < len(4) → "improvement"

  "findings" (both lists):
    baseline = ["Shape: 950 rows"] (1 item)
    current = ["Shape: 1000 rows", "Grain: [order_id]", "Freshness: 2.5h"] (3 items)
    Different! → _classify_list_change("findings", ..., ...)
      "finding" in "findings" → Yes, len(3) > len(1) → "improvement"

  diffs = [
    {"field": "metrics.risk_count", "baseline": 4, "current": 2, "verdict": "improvement", ...},
    {"field": "risks", "baseline": "[4 items]", "current": [{...}], "verdict": "improvement", ...},
    {"field": "findings", "baseline": ["Shape: 950 rows"], "current": "[3 items]", "verdict": "improvement", ...},
  ]

═══ Phase 3: Classify & Summarize ═══

Step 6: Count verdicts
  improvements = [3 entries]
  regressions = []
  unchanged = [row_count, high_null_column_count, staleness_hours entries]

Step 7: Build summary
  → "3 improvement(s), 0 regression(s), 3 unchanged vs baseline"

Step 8: Build metrics
  {
    "improvements_count": 3,
    "regressions_count": 0,
    "unchanged_count": 3,
    "baseline_age_hours": 4.0
  }

═══ Phase 4: Findings, Risks, Suggestions ═══

Step 9: Findings (first 5 diffs)
  ["3 improvement(s), 0 regression(s), 3 unchanged vs baseline",
   "[IMPROVEMENT] metrics.risk_count: 4 → 2",
   "[IMPROVEMENT] risks: [4 items] → [{...}]",
   "[IMPROVEMENT] findings: [Shape: 950 rows] → [3 items]"]

Step 10: Risks
  regressions_count == 0 → no regression risk
  baseline_age_hours (4.0) > 168? No → no staleness risk
  risks = []

Step 11: Suggested actions
  No regressions → skip regression-specific suggestions
  improvements_count > 0 and not save_as_baseline:
    → "Save as new baseline to lock in improvements."

═══ Final Output ═══

{
    "kind": "dogfood_regression_context",
    "subject": "exploration_context::catalog.schema.orders",
    "summary": "3 improvement(s), 0 regression(s), 3 unchanged vs baseline",
    "metrics": {"improvements_count": 3, "regressions_count": 0, ...},
    "findings": [...],
    "risks": [],
    "diffs": [...],
    "baseline_path": ".dogfood_baselines/exploration_context__catalog_schema_orders.json",
    "samples": {},
    "suggested_next_actions": [...]
}
```

---

## Python Patterns

### 1. stdlib Only — No External Dependencies

```python
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
```

**Why:** This module operates on dict structures and JSON files — no DataFrames,
no pandas, no numpy. By depending only on stdlib, it can run anywhere Python runs
(CI pipelines, pre-commit hooks, non-Databricks environments) without install overhead.

### 2. `_slugify` with `re.sub` for Safe Filenames

```python
def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", text)
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_").lower()
```

**Why:** Baseline filenames are derived from `kind` + `subject` which may contain
dots, slashes, spaces, or unicode. Two regex passes (replace non-alphanumeric,
collapse repeated underscores) produce safe, readable filenames. The `.lower()`
normalization prevents case-sensitivity issues on case-insensitive filesystems.

### 3. `time.time()` Epoch for Age Calculation

```python
# Save
baseline = {
    "saved_at_epoch": time.time(),
    ...
}

# Load and compute age
def _baseline_age_hours(baseline: dict) -> float:
    saved_epoch = baseline.get("saved_at_epoch")
    if saved_epoch is None:
        return 0.0
    return (time.time() - saved_epoch) / 3600.0
```

**Why:** Epoch floats are timezone-agnostic, comparison-friendly, and survive
JSON round-trips without parsing. The ISO `saved_at` string is kept for
human readability, but `saved_at_epoch` is what the code actually uses.

### 4. `_COMPARE_FIELDS` List Controls Deep Comparison

```python
_COMPARE_FIELDS = ["metrics", "grain_analysis", "freshness", "risks", "findings"]
```

**Why:** Rather than comparing the entire output dict (which includes volatile
fields like `samples`, `suggested_next_actions`, `parameters`), only semantically
meaningful fields are compared. Adding a new comparable field is a one-line change
to this list — no logic changes needed.

### 5. `_METRIC_DIRECTIONS` Dict for Declarative Classification

```python
_METRIC_DIRECTIONS: dict[str, str] = {
    "null_pct": "lower",
    "risk_count": "lower",
    "completeness_pct": "higher",
    ...
}
```

**Why:** This enables directional classification without per-field `if/elif`
chains. The `_classify_change` function looks up the field name, checks the
direction, and applies a single comparison. Adding a new metric with known
direction is a one-line dict addition — no branching logic changes.

---

## Key Design Decisions

| Decision | Rationale |
| --- | --- |
| stdlib only (no pandas/spark) | Module operates on dicts, not DataFrames — keep dependency graph minimal |
| JSON baselines (not database) | Simple, portable, git-trackable, human-readable |
| `_slugify` for filenames | Handles arbitrary subject strings safely |
| `_COMPARE_FIELDS` whitelist | Prevents false diffs from volatile fields (samples, timestamps) |
| "unchanged" as default verdict | Unknown fields should not trigger alerts; only known-direction fields classify |
| `_summarize_value` for long lists | Prevents multi-KB diff entries for large list fields |
| Epoch time for age, ISO for display | Epoch is machine-friendly; ISO is human-friendly; store both |
| Findings increase = improvement | More observations from a tool means it found more useful information |