# Data Onboarding — CSV Sources

> Preserved source-format technique.

CSV files look simple but hide encoding issues, inconsistent quoting, embedded newlines,
and phantom columns that corrupt ingestion silently.

**You were routed here from `skills/data-onboarding/SKILL.md`.** Return there after completing
Phase 1-2 to continue with Phase 3 (profiling).

## Step 1: Detect Encoding and Structure

**ALWAYS inspect the raw file before reading into a DataFrame.**

```python
# Detect encoding
import chardet
with open("/path/to/file.csv", "rb") as f:
    raw = f.read(10000)
    detection = chardet.detect(raw)
    print(f"Encoding: {detection['encoding']} (confidence: {detection['confidence']:.0%})")

# Inspect first few lines — check delimiter, header, quoting
with open("/path/to/file.csv", encoding=detection["encoding"]) as f:
    for i, line in enumerate(f):
        if i < 5:
            print(f"Line {i}: {repr(line.rstrip())}")
        else:
            break
```

**What to look for:**
- Encoding: UTF-8, UTF-8-BOM, Latin-1, Windows-1252
- Delimiter: comma, tab, pipe, semicolon
- Header: does line 0 look like column names or data?
- Quoting: are fields quoted? consistently?
- BOM marker: `\ufeff` prefix on first field

## Step 2: Record Source Facts

```
SOURCE FACTS (CSV):
├── File: [name.csv] ([size])
├── Encoding: [UTF-8|Latin-1|etc.] (confidence: [N]%)
├── Delimiter: [comma|tab|pipe|semicolon]
├── Header: [line 0 = headers | no header detected]
├── BOM: [yes|no]
├── Quoting: [all fields|some fields|none]
├── Line endings: [\\n|\\r\\n|mixed]
└── Estimated rows: [N] (from file size / avg line length)
```

## Step 3: Read into DataFrame

```python
df = (
    spark.read
    .option("encoding", "utf-8")       # from Step 1 detection
    .option("header", True)             # from Step 1 inspection
    .option("delimiter", ",")           # from Step 1 inspection
    .option("inferSchema", False)       # ALL STRING — type later in silver
    .csv("/path/to/file.csv")
)

print(f"📥 Read {df.count():,} rows × {len(df.columns)} cols")
```

For BOM-encoded files:
```python
df = spark.read.option("encoding", "utf-8-sig").option("header", True).csv("/path/to/file.csv")
```

## Step 4: Verify the Read

```python
# 1. Row count sanity
print(f"Rows: {df.count():,}")

# 2. Column count — any phantom columns?
print(f"Columns ({len(df.columns)}): {df.columns}")
# Look for unnamed columns: '', '_c0', 'Unnamed: 0', etc.

# 3. First/last rows — data aligned correctly?
df.show(5, truncate=False)

# 4. Structural check
anchor("profile_table", df, subject="source_name")
```

## CSV-Specific Gotchas

| Problem | How to detect | Solution |
|---|---|---|
| Wrong encoding (mojibake) | Garbled characters in output | Re-read with correct encoding from chardet |
| BOM marker | First column name has `\ufeff` prefix | Read with `encoding='utf-8-sig'` |
| Embedded newlines | Row count much higher than expected | Use proper CSV parser with `multiLine=True` |
| Mixed line endings | `\r` characters in values | Normalize: `REGEXP_REPLACE(col, '\\r', '')` |
| Phantom columns at end | Columns named `_c0`, empty, or `Unnamed: N` | Drop columns matching phantom patterns |
| Trailing commas | Last column is always empty | Drop the trailing empty column |
| Inconsistent quoting | Quoted fields have literal quotes inside | Use `escape='"'` or `escape='\\'` option |
| Semicolon delimiter | Common in European CSVs | Set `delimiter=';'` |
| Tab delimiter (.tsv) | Fields appear as one column | Set `delimiter='\\t'` |
| No header row | First row is data, not headers | Set `header=False`, assign column names manually |
| Numeric IDs read as integers | Leading zeros stripped (ZIP codes, IDs) | `infer_schema=False` preserves as STRING |
| Date format ambiguity (01/02/03) | Can't tell MM/DD/YY vs DD/MM/YY | Check with `anchor("microscope")` in Phase 3, confirm with domain knowledge |

## Phase 1-2 Gate

✅ You have a DataFrame where:
- Row count is reasonable (no embedded newline inflation)
- Column names are actual headers (no BOM prefix, no phantom columns)
- All columns are STRING type (bronze convention)
- No mojibake — characters display correctly
- `anchor("profile_table")` confirms structural integrity

❌ If any of these fail → fix before proceeding to Phase 3.

**Return to `skills/data-onboarding/SKILL.md` Phase 3.**

## What NOT to Do

| Anti-pattern | Why | Do instead |
|---|---|---|
| NEVER assume UTF-8 encoding | CSVs from legacy systems use latin-1, cp1252 | Detect with `chardet` or read first 1000 bytes |
| NEVER skip header validation | Row 1 may be a title, not column names | Inspect first 5 rows before setting `header=True` |
| NEVER use `inferSchema=true` in production | Type inference is non-deterministic across files | Read as STRING, TRY_CAST in silver |
| NEVER CAST on raw CSV columns | Embedded spaces, BOM markers, empty strings | Use `TRY_CAST(NULLIF(TRIM(col), ''))` |
| NEVER skip delimiter detection | Tabs, pipes, semicolons are common | Check with `csv.Sniffer` or `anchor("profile_table")` |
| NEVER write ad-hoc cleaning code | Anchor has tested transforms | `anchor("transform")` → `anchor("apply_transform")` |
