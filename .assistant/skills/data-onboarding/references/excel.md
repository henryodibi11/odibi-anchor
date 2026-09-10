# Data Onboarding — Excel Sources

> Preserved source-format technique.

Excel is the most dangerous source type. Merged cells, hidden sheets, title rows above headers,
multiple tables per sheet, formulas that resolve to different values — all of these corrupt
ingestion if you read blindly.

**You were routed here from `skills/data-onboarding/SKILL.md`.** Return there after completing
Phase 1-2 to continue with Phase 3 (profiling).

## Step 1: Profile the Workbook BEFORE Reading Data

**NEVER read an Excel file into a DataFrame without profiling first.**

Use a workbook reader available in the current environment. Prefer packaged/common
libraries (`openpyxl` for `.xlsx`/`.xlsm`, `xlrd` where supported for `.xls`) and inspect
workbook metadata before materializing tables. Enumerate visible and hidden sheets,
dimensions, merged ranges, formulas versus cached values, and sample candidate header
rows. If no capable reader is installed, report that capability gap rather than importing
from a private workspace path.

```python
from pathlib import Path
import openpyxl

path = Path("/path/to/file.xlsx")
wb = openpyxl.load_workbook(path, data_only=False, read_only=False)
inspection = []
for ws in wb.worksheets:
    sample = list(ws.iter_rows(min_row=1, max_row=min(ws.max_row, 20), values_only=True))
    inspection.append({
        "sheet": ws.title,
        "state": ws.sheet_state,
        "rows": ws.max_row,
        "columns": ws.max_column,
        "merged_ranges": [str(item) for item in ws.merged_cells.ranges],
        "formula_cells": sum(
            1 for row in ws.iter_rows() for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=")
        ),
        "candidate_header_rows": sample,
    })
print(inspection)
```

## Step 2: Extract Source Facts from Profile

Read the profile output and record:

```
SOURCE FACTS (Excel):
├── File: [name.xlsx] ([N] sheets, [N] cells)
├── Data sheet: [sheet_name] ([N] rows × [N] cols)
│   └── Classification: [data|summary|dashboard|lookup]
├── Header row: [N] (skip [N] title rows above)
├── Candidate keys: [col1, col2] (verify after DataFrame profiling)
├── Columns with formulas: [list]
├── Columns with mixed types: [list]
├── Sheets to ignore: [summary, lookup, dashboard sheets]
├── Merged cells: [locations — will these corrupt headers?]
├── Totals row: [yes/no — exclude from data if yes]
├── Cross-sheet refs: [which sheets feed which]
└── Hidden sheets/columns: [review before ignoring]
```

**Key decisions from the profile:**
- Which sheet(s) to read (data sheets only, not summaries/dashboards)
- What `header_row` and `skip_rows` to set when reading
- Whether merged cells will corrupt column headers (manual mapping needed?)
- Whether to exclude a totals row at the bottom

## Step 3: Read into DataFrame

```python
import pandas as pd

# Use the Step 1 inspection findings to set read options
pdf = pd.read_excel(
    "/path/to/file.xlsx",
    sheet_name="DATA_SHEET",      # from Step 2 — the right sheet
    header=2,                      # from Step 2 — actual header position
    dtype=str,                     # bronze starts as STRING
)
expected_rows = len(pdf)
df = spark.createDataFrame(pdf)

print(f"📥 Read {df.count():,} rows × {len(df.columns)} cols")
```

For merged cells or other workbook structures that Pandas cannot represent, use openpyxl:

```python
import openpyxl
import pandas as pd

wb = openpyxl.load_workbook("/path/to/file.xlsx", data_only=True, read_only=True)
ws = wb["DATA_SHEET"]

# Set these from the bounded workbook inspection above.
header_row_idx = 2  # zero-based row index; example only
data = list(ws.iter_rows(min_row=header_row_idx + 2, values_only=True))
headers = [str(c.value).strip() for c in list(ws.iter_rows(
    min_row=header_row_idx + 1, max_row=header_row_idx + 1))[0]]

pdf = pd.DataFrame(data, columns=headers)
pdf = pdf.astype(str).replace("None", None)  # ALL STRING — type later in silver
expected_rows = len(pdf)
df = spark.createDataFrame(pdf)
wb.close()
```

## Step 4: Verify the Read

**ALWAYS verify immediately after reading:**

```python
# 1. Row count — matches the rows materialized from the inspected range?
actual_rows = df.count()
print(f"Expected {expected_rows}, got {actual_rows}")

# 2. Column count — any phantom columns from merged cells?
print(f"Columns ({len(df.columns)}): {df.columns}")

# 3. Quick sanity — headers in right place? (not data in headers)
df.show(5, truncate=False)

# 4. Structural check
anchor("profile_table", df, subject="source_name")
```

## Excel-Specific Gotchas

| Problem | How to detect | Solution |
|---|---|---|
| Header not in row 1 | Candidate-header samples | Set `skip_rows` when reading |
| Multiple tables per sheet | Separated non-empty regions in the sample/range | Read each table separately |
| Dates as serial numbers (44927) | microscope pattern `99999` on date columns | `DATE(1900,1,1) + serial - 2` |
| Numbers with commas ("1,234") | microscope pattern `9,999` | `REGEXP_REPLACE(col, ',', '')` → `TRY_CAST` |
| Currency symbols ($1,234.56) | microscope pattern `$9,999.99` | `REGEXP_REPLACE(col, '[$€£,]', '')` → `TRY_CAST` |
| Zero-width Unicode chars | coerce_check `dominant_category=unicode` | `REGEXP_REPLACE(col, '[\\u200B-\\u200D\\uFEFF]', '')` |
| Trailing/leading whitespace | coerce_check `dominant_category=whitespace` | `TRIM()` — handled by transform `null_cleanup` step |
| Merged cell headers | Inspected `merged_ranges` | Manual header mapping before read |
| Totals row at bottom | Sample final rows and compare formulas/labels | Exclude last row when reading |
| Hidden sheets with data | Inspected sheet `state` | Review hidden content — don't blindly ignore |
| Formula columns | Inspected `formula_cells` plus raw cell values | Read with `data_only=True` for cached values when available |
| Percentage columns (0.15 vs 15%) | microscope shows values 0-1 | Decide: multiply by 100 or keep as decimal |

## Lesson from CRM Dogfooding

Real-world Excel from CRM systems (6454 rows × 55 columns) revealed:
- **4742 unicode mismatches** in `Interconnection Entity` — zero-width spaces invisible to humans
- **664 null_to_value changes** in `Requested COD Year` — nulls filled in new version
- **3 whitespace mismatches** in `Generic Queue Status` — trailing spaces

These are NOT data errors — they're coercion artifacts. When onboarding a refresh of existing
data, ALWAYS run `anchor("diff")` → `anchor("coerce_check")` before assuming changes are real.

## Phase 1-2 Gate

✅ You have a DataFrame where:
- Row count matches `expected_rows` from the inspected range (accounting for explicitly filtered totals rows)
- Column names are actual headers (not data values, not `_c0`, not merged cell artifacts)
- All columns are STRING type (bronze convention — type later in silver)
- `anchor("profile_table")` confirms structural integrity
- You recorded all source facts from the Step 1 inspection output

❌ If any of these fail → fix before proceeding to Phase 3.

**Return to `skills/data-onboarding/SKILL.md` Phase 3.**

## What NOT to Do

| Anti-pattern | Why | Do instead |
|---|---|---|
| NEVER read Excel without inspecting the workbook first | Multiple sheets, merged cells, hidden rows | Run the bounded native-reader inspection above |
| NEVER trust Excel serial dates | 44927 is not a number — it's 2023-01-01 | Detect with `anchor("microscope")`, convert explicitly |
| NEVER assume header is on row 1 | Title rows, blank rows, metadata above header | Inspect first 5 rows, set `skipRows=` appropriately |
| NEVER infer types from Excel formatting | "Number" format may contain text, currency symbols | Read as STRING, TRY_CAST in silver |
| NEVER skip `anchor("profile_table")` after load | Zero-width chars, phantom columns, trailing spaces | Profile catches what eyes miss |
