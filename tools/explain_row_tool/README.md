# Explain Row

> Trace exactly where each column value in a specific output row came from — across multiple upstream DataFrames.

---

## When to Use

- You have a merged or enriched output and one row's values don't look right
- You need to know which upstream DataFrame contributed a specific column value
- A join produced unexpected results and you want to trace the exact match path
- You're debugging a COALESCE or default-fill and need to confirm which source "won"

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

import pandas as pd

# Two upstream DataFrames
source_a = pd.DataFrame({
    "order_id":    [1, 2, 3],
    "customer_id": [101, 102, 103],
    "revenue":     [150.0, 200.0, 75.0],
})
source_b = pd.DataFrame({
    "customer_id": [101, 102, 103],
    "region":      ["North", "South", "East"],
})

# Merged output
output = source_a.merge(source_b, on="customer_id")

# Trace where order_id=1 came from
ctx = anchor("explain_row", output,
         keys=["order_id"],
         values={"order_id": 1},
         upstream={"source_a": source_a, "source_b": source_b})
```

---

## Usage

```python
# From anchor() dispatcher (most common)
ctx = anchor("explain_row", output_df,
         keys=["order_id"],
         values={"order_id": 1},
         upstream={"source_a": df_a, "source_b": df_b})

# With multiple key columns
ctx = anchor("explain_row", output_df,
         keys=["order_id", "line_item"],
         values={"order_id": 42, "line_item": 1},
         upstream={"raw": df_raw, "enriched": df_enriched})
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `output_df` | DataFrame | Yes | The result/output DataFrame containing the row to explain |
| `keys` | list[str] | Yes | Column(s) to identify the row |
| `values` | dict | Yes | Key column values to locate the row (e.g., `{"order_id": 1}`) |
| `upstream` | dict[str, DataFrame] | Yes | Named upstream DataFrames — keys become display names in output |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |
| `sample_limit` | int | No | Max upstream matches to show per column (default 5) |

> Works with both pandas and Spark DataFrames.

---

## Output

```python
ctx["summary"]          # "Row traced through 2 upstream source(s). 3 columns from source_a, 1 column from source_b."
ctx["column_lineage"]   # List of per-column trace results
ctx["join_paths"]       # Which upstream matched on which keys
ctx["findings"]         # Notable matches or mismatches
ctx["risks"]            # Transformed or null-filled values that need investigation
ctx["suggested_next_actions"]  # Next anchor() calls to investigate further
```

Each `column_lineage` entry:
```python
{
    "column":          "revenue",
    "output_value":    150.0,
    "origin":          "source_a",    # which upstream DataFrame
    "upstream_value":  150.0,
    "match_type":      "exact",       # exact | transformed | coerced | null_filled | not_found
    "join_key":        True,
}
```

---

## Reading the Output

**Match types** tell you the relationship between output and upstream value:

| Type | Meaning |
|---|---|
| `exact` | Output value matches upstream exactly |
| `transformed` | Same column exists but value differs — investigate the transform |
| `coerced` | Values match after whitespace/case normalization |
| `null_filled` | Upstream was NULL; output has a value (COALESCE or default applied) |
| `not_found` | Column not present in this upstream — came from elsewhere |

Focus on `transformed` and `null_filled` entries in `ctx["risks"]` — those are the most likely source of data surprises.

---

## Pairs Well With

- `anchor("pre_join", ...)` — check for duplicate upstream keys before tracing
- `anchor("microscope", output_df, "col")` — profile a suspicious column in the output
- `anchor("case_file", output_df, column="col", filter="where:match_type==transformed")` — isolate transformed-value rows
- `anchor("diagnose_empty", ...)` — if the row is missing entirely (output has 0 rows)

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.explain_row_tool.explain_row_impl import explain_row_context

source_a = pd.DataFrame({
    "order_id":    [1, 2, 3],
    "customer_id": [101, 102, 103],
    "revenue":     [150.0, 200.0, 75.0],
})
source_b = pd.DataFrame({
    "customer_id": [101, 102, 103],
    "region":      ["North", "South", "East"],
})
output = source_a.merge(source_b, on="customer_id")

ctx = explain_row_context(
    output,
    keys=["order_id"],
    values={"order_id": 1},
    upstream={"source_a": source_a, "source_b": source_b},
)

ctx["summary"]
# "Row traced through 2 upstream source(s). 3 columns from source_a. 1 column from source_b."

ctx["column_lineage"][0]["match_type"]
# "exact"
```

> **Note:** This tool imports from `odibi_anchor._utils`. The `src/` path is required.

---

## Documentation

Detailed documentation is available in the `docs/` folder:

- [Architecture](docs/architecture.md) — Pipeline design, match types, internal functions, and output contract
- [Usage Guide](docs/usage.md) — Full parameter reference, calling patterns, result structure, and error handling
- [Code Walkthrough](docs/code_walkthrough.md) — Step-by-step execution traces with 2 worked examples (exact+coerced, null-filled+transformed)
