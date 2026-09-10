# error_trace_context

`error_trace_context` turns noisy Python, pandas, Spark, SQL, and Databricks
error tracebacks into a compact, structured context packet for humans and LLMs.

It answers:

```text
What went wrong?
Where did it fail?
What category of error is this?
What should I try next?
```

## Public API

```python
from odibi_anchor.debugging import error_trace_context

def error_trace_context(
    error_text: str | BaseException,
    *,
    subject: str | None = None,
    df: Any | None = None,
    engine: str = "auto",
    max_chars: int = 6000,
    sample_limit: int = 5,
    metadata: Mapping[str, Any] | None = None,
    include_dataframe_sample: bool = True,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `error_text` | str or BaseException | required | Raw traceback text or exception object |
| `subject` | str | None | Workflow, table, notebook, or step label |
| `df` | DataFrame or None | None | Optional DataFrame related to the failure |
| `engine` | str | `"auto"` | DataFrame engine: `"auto"`, `"pandas"`, or `"spark"` |
| `max_chars` | int | 6000 | Maximum characters in `relevant_trace` (min 200) |
| `sample_limit` | int | 5 | Max trace frames and DataFrame rows to include |
| `metadata` | Mapping or None | None | Run metadata (job_id, notebook_path, table, etc.) |
| `include_dataframe_sample` | bool | True | Include sample rows from pandas DataFrame |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Output Shape

```python
{
    "kind": "error_trace_context",
    "subject": "silver.project_fact",
    "summary": "KeyError in dataset_profile_context.py:245 — likely schema_or_column_reference error",
    "metrics": {
        "input_char_count": 1250,
        "input_line_count": 28,
        "relevant_trace_char_count": 800,
        "trace_frame_count": 3,
        "exception_chain_count": 1,
        "max_chars": 6000,
        "has_dataframe_context": True,
        "dataframe_engine": "pandas",
    },
    "error": {
        "type": "KeyError",
        "message": "'missing_column'",
        "category": "schema_or_column_reference",
        "exception_chain": [
            {"type": "KeyError", "message": "'missing_column'"},
        ],
    },
    "location": {
        "file": "dataset_profile_context.py",
        "line": 245,
        "function": "_profile_column",
    },
    "trace_frames": [
        {"file": "...", "line": 100, "function": "main"},
        {"file": "dataset_profile_context.py", "line": 245, "function": "_profile_column"},
    ],
    "dataframe_context": {
        "engine": "pandas",
        "shape": [100, 5],
        "columns": ["id", "name", "amount", "date", "status"],
        "dtypes": {"id": "int64", "name": "object", ...},
        "missing_columns": ["missing_column"],
        "sample_rows": [...],
    },
    "metadata": {"notebook_path": "/Users/me/pipeline.py"},
    "findings": [...],
    "risks": [...],
    "samples": [...],
    "relevant_trace": "Traceback (most recent call last):\n...",
    "suggested_next_actions": [...],
}
```

### Key Output Fields

- **`error["type"]`** — Exception class name (e.g., `"KeyError"`, `"AnalysisException"`).
- **`error["message"]`** — The error message text.
- **`error["category"]`** — Broad debugging category (see table below).
- **`error["exception_chain"]`** — List of chained exceptions (outermost first).
- **`location`** — File, line, and function of the likely failure point.
- **`trace_frames`** — Parsed stack frames (capped by `sample_limit`).
- **`dataframe_context`** — DataFrame schema/sample when `df` is provided.
- **`dataframe_context["missing_columns"]`** — Columns referenced in error but not in df.
- **`relevant_trace`** — Compact traceback excerpt (capped by `max_chars`).
- **`suggested_next_actions`** — Actionable fix suggestions based on category.

## Error Categories

| Category | Triggered By |
|----------|-------------|
| `syntax` | SyntaxError, ParseException |
| `schema_or_column_reference` | KeyError, UNRESOLVED_COLUMN, cannot resolve |
| `type_conversion` | ValueError (convert), TypeError, cannot cast |
| `missing_dependency` | ModuleNotFoundError, ImportError |
| `file_or_path` | FileNotFoundError, path does not exist |
| `permission` | PermissionError, access denied, forbidden |
| `merge_or_key` | MergeError, duplicate keys in join |
| `execution_resource` | OutOfMemory, ExecutorLostFailure, timeout |
| `data_quality` | AssertionError, validation, constraint |
| `unknown` | None of the above matched |

## Example Usage

### Basic (Exception Object)

```python
import pandas as pd
from odibi_anchor.debugging import error_trace_context

df = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})

try:
    df["missing_column"]
except Exception as exc:
    ctx = error_trace_context(exc, df=df, subject="silver.customers")

print(ctx["error"]["type"])        # "KeyError"
print(ctx["error"]["category"])    # "schema_or_column_reference"
print(ctx["dataframe_context"]["missing_columns"])  # ["missing_column"]
```

### Raw Traceback Text

```python
traceback_text = """
Traceback (most recent call last):
  File "pipeline.py", line 42, in run
    result = df.merge(other, on="customer_id", validate="one_to_one")
pandas.errors.MergeError: Merge keys are not unique in right dataset
"""

ctx = error_trace_context(traceback_text, subject="merge_step")
print(ctx["error"]["category"])  # "merge_or_key"
```

### With Run Metadata

```python
try:
    run_pipeline(df)
except Exception as exc:
    ctx = error_trace_context(
        exc,
        df=df,
        subject="silver.transactions",
        metadata={
            "job_id": "12345",
            "notebook_path": "/Users/me/pipelines/ingest.py",
            "step": "silver_merge",
        },
    )
```

### Markdown Report for LLM Prompt

```python
try:
    transform(df)
except Exception as exc:
    report = error_trace_context(
        exc, df=df, output_format="markdown"
    )
    # Feed directly to LLM for diagnosis assistance
```

### Standalone Render Function

```python
from odibi_anchor.debugging import error_trace_context, render_error_trace_report

# Two-step: inspect dict, then render
ctx = error_trace_context(exc, df=df)
if ctx["error"]["category"] == "schema_or_column_reference":
    print(render_error_trace_report(ctx))
```

### Pipeline Error Handler

```python
from odibi_anchor.debugging import error_trace_context

def handle_pipeline_error(exc, df=None, step_name="unknown"):
    ctx = error_trace_context(exc, df=df, subject=step_name)
    
    # Log structured context
    log_error(ctx["summary"], ctx["error"]["category"])
    
    # Provide fix guidance
    for action in ctx["suggested_next_actions"]:
        print(f"  → {action}")
    
    return ctx
```

## DataFrame Context

When `df` is provided, the tool generates column-aware diagnostics:

- **Pandas DataFrames** — Summarized immediately (shape, dtypes, columns, sample rows).
- **Spark DataFrames** — Column list extracted; full summarization reserved for
  Databricks-native follow-up (API preserved).
- **`missing_columns`** — Columns mentioned in the error message but not present
  in the DataFrame. Critical for diagnosing schema/column reference errors.

Set `include_dataframe_sample=False` to omit row samples (useful for PII-sensitive data).

## Metadata Keys

Only compact scalar values from these keys are retained:

```
job_id, run_id, task_key, notebook_path, pipeline, step,
table, source_table, target_table, user, environment
```

Other keys are silently discarded.

## Performance Notes

- **Zero external dependencies** — stdlib only (re, traceback, typing).
- **Deterministic** — No API calls, no model inference, no randomness.
- **Fast** — Regex-based parsing; sub-millisecond for typical tracebacks.
- **max_chars cap** — Prevents bloated context from multi-page stack traces.
- **sample_limit cap** — Limits frame and row extraction for bounded output size.

## When To Use

Use this:

- in `try/except` blocks to generate structured error context;
- as input to LLM-based debugging agents;
- in pipeline error handlers for structured logging;
- to diagnose column reference errors with DataFrame awareness;
- when an error traceback is too noisy to read directly.

## Gotchas

- Passing an exception object is preferred (carries `__traceback__`), but raw
  text strings also work (parsed via regex).
- `error["category"]` is a heuristic classification — it may be `"unknown"` for
  rare or custom exceptions.
- `dataframe_context` is None when no `df` is passed.
- `location` may be None if no stack frame could be extracted.
- `max_chars` minimum is 200; raises `ValueError` if lower.
- `sample_limit` must be ≥ 0; raises `ValueError` if negative.
- `output_format` must be `"dict"` or `"markdown"`; raises `ValueError` otherwise.
