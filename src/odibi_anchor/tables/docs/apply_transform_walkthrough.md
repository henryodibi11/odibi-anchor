# apply_transform_context — Code Walkthrough

**Module:** `apply_transform_context.py` (1,059 lines)  
**Purpose:** Execute a transform plan programmatically — no `exec()`. Action-to-handler dispatch.

---

## How It Works

`apply_transform_context` takes a DataFrame and a transform plan (produced by `transform_plan_context`) and applies each step sequentially. Key design principles:

- **No `exec()` or `eval()`** — uses a dispatch dict mapping action names to handler functions
- **Per-step error isolation** — one step failing doesn't block subsequent steps
- **Checkpoint support** — caches intermediate results for rollback capability
- **Audit trail** — records what happened at each step (rows before/after, failures)

---

## Worked Example 1: Apply Rename Step

### Input

```python
df = pd.DataFrame({
    "Customer Name": ["Alice", "Bob", "Charlie"],
    "amount": [100, 200, 300]
})

plan = {
    "steps": [{
        "order": 1,
        "action": "standardize",
        "column": "Customer Name",
        "target": "customer_name"
    }]
}
```

### Step-by-Step Execution

**Step 1: Dispatch to handler**

The function looks up the action in its handler registry:

```python
action_handlers = {
    "standardize": _apply_standardize_pandas,
    "cast": _apply_cast_pandas,
    "trim": _apply_trim_pandas,
    "dedup": _apply_dedup_pandas,
    "fill_null": _apply_fill_null_pandas,
    # ... more handlers
}

handler = action_handlers["standardize"]  → _apply_standardize_pandas
```

**Step 2: Execute handler**

`_apply_standardize_pandas(df, step)` performs:

```python
df = df.rename(columns={"Customer Name": "customer_name"})
```

**Step 3: Verify output**

Post-step validation:
- Does `"customer_name"` exist in output columns? Yes ✓
- Does `"Customer Name"` still exist? No ✓ (renamed, not duplicated)
- Row count unchanged? 3 == 3 ✓

**Step 4: Record result**

```python
step_result = {
    "step_order": 1,
    "action": "standardize",
    "status": "applied",
    "rows_before": 3,
    "rows_after": 3,
    "column": "Customer Name",
    "target": "customer_name"
}
```

### Output

```python
{
    "kind": "apply_transform",
    "df": DataFrame,  # transformed DataFrame with "customer_name" column
    "metrics": {
        "steps_total": 1,
        "steps_applied": 1,
        "steps_failed": 0,
        "rows_before": 3,
        "rows_after": 3
    },
    "step_results": [step_result],
    "checkpoints": {}  # checkpoint_name → cached DataFrame
}
```

---

## Worked Example 2: Apply Cast with Error Isolation

### Input

```python
df = pd.DataFrame({
    "price": ["100.50", "200.00", "N/A", "invalid", "350.75",
              "99.99", "150.00", "abc", "275.50", "400.00"]
})

plan = {
    "steps": [{
        "order": 3,
        "action": "cast",
        "column": "price",
        "target_type": "float"
    }]
}
```

### Step-by-Step Execution

**Step 1: Dispatch to cast handler**

```python
handler = action_handlers["cast"]  → _apply_cast_pandas
```

**Step 2: Execute cast with coercion**

`_apply_cast_pandas(df, step)` uses `errors="coerce"` to handle unparseable values:

```python
df["price"] = pd.to_numeric(df["price"], errors="coerce")
```

Result:
- `"100.50"` → 100.50
- `"200.00"` → 200.00
- `"N/A"` → NaN (coercion failure)
- `"invalid"` → NaN (coercion failure)
- `"350.75"` → 350.75
- `"99.99"` → 99.99
- `"150.00"` → 150.00
- `"abc"` → NaN (coercion failure)
- `"275.50"` → 275.50
- `"400.00"` → 400.00

**Step 3: Count coercion failures**

```python
coercion_failures = df["price"].isna().sum()  # 3 values couldn't convert
rows_affected = len(df) - coercion_failures   # 7 successfully cast
```

**Step 4: Record result with failure detail**

```python
step_result = {
    "step_order": 3,
    "action": "cast",
    "status": "applied",  # applied even with some failures
    "column": "price",
    "target_type": "float",
    "coercion_failures": 3,
    "rows_affected": 7,
    "failure_rate": 0.3,
    "sample_failures": ["N/A", "invalid", "abc"]
}
```

Note: The step status is `"applied"` (not `"failed"`) because the cast succeeded — some values simply couldn't be converted. This is expected behavior with `errors="coerce"`.

### Output

```python
{
    "kind": "apply_transform",
    "df": DataFrame,  # price column is now float64 with 3 NaN values
    "metrics": {
        "steps_total": 1,
        "steps_applied": 1,
        "steps_failed": 0,
        "coercion_failures": {"price": 3}
    },
    "step_results": [step_result],
    "findings": [
        "Cast 'price' to float: 3 values could not be converted (became NaN)",
        "Sample failures: ['N/A', 'invalid', 'abc']"
    ],
    "risks": [
        "30% coercion failure rate on 'price' — review source data quality"
    ]
}
```

---

## Python Patterns

- **Action-to-handler dispatch dict** — maps action strings to handler functions, avoiding `exec()`/`eval()` while remaining extensible (add a new handler = add one dict entry)
- **Per-step error isolation** — each step is wrapped in try/except; a failed step records `status: "failed"` with the error message but doesn't abort the entire plan
- **Checkpoint support for Spark** — at configurable intervals (default: every 3 steps), caches the intermediate DataFrame; enables `anchor("rollback", result, to="step_name")` to revert to any checkpoint
- **`_format_bytes` for human-readable memory estimates** — converts byte counts to KB/MB/GB for checkpoint size reporting
- **Immutable step execution** — handlers receive a copy of the DataFrame (or a new Spark plan); the original is preserved for rollback
- **Audit-ready output** — every step records rows_before/rows_after, enabling drift detection (did a step unexpectedly change row count?)
