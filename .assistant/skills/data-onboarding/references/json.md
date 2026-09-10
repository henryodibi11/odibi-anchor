# Data Onboarding — JSON / JSONL Sources

> Preserved source-format technique.

JSON files look self-describing but hide schema inconsistencies, deep nesting,
mixed types, and encoding issues that corrupt ingestion silently.

**You were routed here from `skills/data-onboarding/SKILL.md`.** Return there after completing
Phase 1-2 to continue with Phase 3 (profiling).

## Step 1: Profile the File Structure

**ALWAYS inspect structure before reading into a DataFrame.** Use Python's packaged
`json` library for JSON and line-by-line decoding for JSONL. The JSONL path below keeps
only a bounded sample in memory while counting records. The standard-library JSON path
materializes one top-level value; use it only when that value fits memory. For larger
monolithic JSON, use an available packaged streaming parser or report the capability gap.
Inventory sampled top-level keys/types and measure schema consistency before selecting a
flattening strategy. Inspect nested values separately when deciding how to flatten them.

```python
import json
from pathlib import Path

path = Path("/path/to/file.json")
sample_limit = 1000
if path.suffix.casefold() == ".jsonl":
    sample = []
    record_count = 0
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL record on line {line_number}") from exc
            record_count += 1
            if len(sample) < sample_limit:
                sample.append(record)
else:
    with path.open(encoding="utf-8-sig") as stream:
        value = json.load(stream)
    records = value if isinstance(value, list) else [value]
    record_count = len(records)
    sample = records[:sample_limit]

key_sets = [frozenset(row) for row in sample if isinstance(row, dict)]
all_keys = sorted(set().union(*key_sets)) if key_sets else []
schema_consistency = (
    100 * max((key_sets.count(keys) for keys in set(key_sets)), default=0) / len(key_sets)
    if key_sets else 0
)
types_by_key = {
    key: sorted({type(row[key]).__name__ for row in sample if isinstance(row, dict) and key in row})
    for key in all_keys
}
profile = {
    "record_count": record_count,
    "sample_count": len(sample),
    "keys": all_keys,
    "schema_consistency_percent": schema_consistency,
    "types_by_key": types_by_key,
}
print(profile)
```

**What the profiler tells you:**
- Format: JSON (single array/object) or JSONL (line-delimited)
- Whether UTF-8/UTF-8-BOM decoding succeeds
- Total record count and bounded sample count
- Schema consistency: what % of records share the same key set
- Sampled top-level keys and their observed Python value types

Derive candidate keys, nullability, nesting depth, and array shapes with explicit follow-up
checks against the sample; the snippet does not claim to calculate them.

## Step 2: Record Source Facts

```
SOURCE FACTS (JSON):
|-- File: [name.json] ([size])
|-- Format: [json|jsonl]
|-- Encoding: [UTF-8|Latin-1|etc.]
|-- Records: [N]
|-- Max Depth: [N]
|-- Schema Consistency: [N]%
|-- Top-level fields: [N]
|-- Candidate Keys: [field1, field2, ...]
|-- Nested Paths: [path1, path2, ...]
|-- Warnings: [list any schema issues]
```

## Step 3: Choose a Nested-Data Strategy

Spark preserves nested JSON objects as struct columns; it does not automatically flatten
them into dotted or underscored top-level columns. Decide which nested fields the silver
model needs before projecting structs or exploding arrays. This sample-only helper lets
you compare possible object projections without changing the bronze read:

| Depth | Behavior | Use when |
|---|---|---|
| 0 | Top-level keys only, nested objects stay opaque | You want to handle nesting manually |
| 1 | One level flattened (e.g. `address.city`) | Most common — good default |
| 2+ | Deeper flattening (e.g. `location.coords.lat`) | Deeply nested API responses |

```python
def flatten(record, *, depth, prefix=""):
    result = {}
    for key, value in record.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and depth > 0:
            result.update(flatten(value, depth=depth - 1, prefix=path))
        else:
            result[path] = value
    return result

# Compare bounded samples at each depth to decide.
for depth in [0, 1, 2]:
    columns = set().union(*(
        flatten(row, depth=depth) for row in sample if isinstance(row, dict)
    )) if sample else set()
    cols = len(columns)
    print(f"  depth={depth}: {cols} columns")
```

**Rule of thumb:** Keep raw nested structures in bronze. In silver, explicitly project the
required struct fields and explode only arrays whose target grain requires it. The helper
above explores the sampled shape; it is not applied to the bronze DataFrame below.

## Step 4: Read into DataFrame

```python
# For JSON files (array of objects)
df = spark.read.option("multiLine", True).json("/path/to/file.json")

# For JSONL files (one record per line)
df = spark.read.option("multiLine", False).json("/path/to/file.jsonl")

print(f"Read {df.count():,} rows x {len(df.columns)} cols")
```

**Exploration convention:** The snippet above lets Spark infer a schema for initial
inspection. For a production bronze ingestion, supply and version an explicit schema once
the source contract is known; apply business typing and flattening in silver.

## Step 5: Verify the Read

```python
# 1. Row count matches profiler
assert df.count() == profile["record_count"], "Row count mismatch!"

# 2. Top-level alignment; nested objects should remain struct columns in bronze
print(f"Spark columns ({len(df.columns)}): {df.columns}")

# 3. First rows look correct
df.show(5, truncate=False)

# 4. Structural check
anchor("profile_table", df, subject="source_name")
```

## Step 6: Handle Schema Inconsistency

If the profiler reports schema consistency < 100%:

```python
# Compare each key's presence count — low presence = field only in some records
presence = {
    key: sum(isinstance(row, dict) and key in row for row in sample)
    for key in profile["keys"]
}
print(presence)

# Spark handles this automatically (missing fields become null)
# But document which fields are optional vs required
```

**Decision:**
- If < 90% consistency: investigate why schemas differ (versioned API? multiple sources?)
- If 90-99% consistency: likely optional fields — document and proceed
- If 100% consistency: clean schema — proceed normally

## JSON-Specific Gotchas

| Problem | How to detect | Solution |
|---|---|---|
| Wrong encoding (mojibake) | Profiler raises ValueError or garbled samples | Re-profile with `encoding="latin-1"` |
| Schema inconsistency | Profiler shows < 100% consistency | Document optional fields, handle nulls |
| Deep nesting | Sample inspection shows nested objects at multiple levels | Project only required struct fields in silver |
| Array fields | Columns with type "array" | Use `explode()` in silver layer |
| Mixed types in one field | Column type = "mixed" | Cast to string in bronze, parse in silver |
| Single object (not array) | record_count = 1 | Verify this is expected — may need manual handling |
| Numeric IDs inferred inconsistently | Inferred type differs across files or values | Define the production schema explicitly; cast only after validating the ID contract |
| Datetime as strings | Column type = "datetime" | TRY_CAST to TIMESTAMP in silver |
| Large JSONL file | Slow profiling | Stream all lines for count while retaining only `sample_limit` records |
| Large monolithic JSON | Standard `json.load` requires the value in memory | Use an available streaming parser or report the capability gap |
| BOM marker | Encoding = "utf-8-sig" | Spark handles BOM automatically |
| Nested arrays of objects | Array contains dicts | Needs `explode()` + `from_json()` in silver |

## Phase 1-2 Gate

You have a DataFrame where:
- Row count matches profiler's record_count
- Top-level column names are reasonable and expected nested objects remain struct columns
- Schema consistency issues are documented
- Encoding is correct (no mojibake in sample values)
- `anchor("profile_table")` confirms structural integrity

If any of these fail -> fix before proceeding to Phase 3.

**Return to `skills/data-onboarding/SKILL.md` Phase 3.**

## What NOT to Do

| Anti-pattern | Why | Do instead |
|---|---|---|
| NEVER assume consistent schema across records | JSON allows mixed schemas within one file | Profile first, check schema consistency |
| NEVER flatten deeply nested JSON blindly | Over-flattening creates hundreds of sparse columns | Choose flatten depth based on `anchor("profile_table")` |
| NEVER use `inferSchema` on JSON in production | Nulls cause type inference to pick wrong types | Read with explicit schema or as STRING |
| NEVER ignore array fields | Arrays need `explode()` or JSON extraction strategy | Profile array lengths, decide: explode vs extract |
| NEVER skip encoding detection for JSONL | Files may have BOM or mixed encoding | Check first bytes, strip BOM if present |
| NEVER write ad-hoc parsing code | Anchor has tested transforms | `anchor("transform")` → `anchor("apply_transform")` |
