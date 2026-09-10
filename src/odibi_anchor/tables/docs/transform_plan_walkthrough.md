# transform_plan_context — Code Walkthrough

**Module:** `transform_plan_context.py` (983 lines)  
**Purpose:** Generate a reviewable transform plan with executable Spark + Pandas code from a profile dict.

---

## How It Works

`transform_plan_context` takes a profile dict (output from `anchor("profile_table")`) and generates a structured transform plan. The plan contains ordered steps, each with:

- An action type (standardize, cast, trim, dedup, etc.)
- Confidence score
- Executable code for both Spark and Pandas
- Human-readable reason

Critically, this module operates on **profile dicts only** — it never touches a DataFrame. The actual execution happens in `apply_transform_context`.

---

## Worked Example 1: Column Rename Step

### Input

```python
profile = {
    "columns": {
        "Customer Name": {
            "dtype": "object",
            "null_rate": 0.02,
            "unique_rate": 0.85,
            "sample_values": ["Alice Smith", "Bob Jones", "Charlie Brown"]
        }
    }
}
```

### Step-by-Step Execution

**Step 1: Check if column needs standardization**

`_needs_snake_case("Customer Name")` evaluates:
- Has space? Yes → needs rename
- Has uppercase? Yes → needs rename
- Result: True

**Step 2: Generate snake_case name**

`_to_snake_case("Customer Name")` transforms:
1. Replace spaces with underscores: `"Customer_Name"`
2. Insert underscore before uppercase transitions: `"Customer_Name"` (already separated)
3. Lowercase everything: `"customer_name"`

**Step 3: Generate the step**

```python
step = {
    "order": 1,
    "action": "standardize",
    "column": "Customer Name",
    "target": "customer_name",
    "confidence": 0.9,  # _HIGH_CONFIDENCE
    "code_spark": '.withColumnRenamed("Customer Name", "customer_name")',
    "code_pandas": '.rename(columns={"Customer Name": "customer_name"})',
    "reason": "Column name contains spaces/uppercase"
}
```

### Output (single-step plan)

```python
{
    "kind": "transform_plan",
    "subject": "data_cleanup",
    "metrics": {
        "total_steps": 1,
        "high_confidence_steps": 1,
        "low_confidence_steps": 0
    },
    "steps": [step],
    "findings": ["1 column needs standardization: 'Customer Name' → 'customer_name'"],
    "risks": []
}
```

---

## Worked Example 2: Type Cast Step

### Input

```python
profile = {
    "columns": {
        "amount": {
            "dtype": "object",  # stored as text
            "null_rate": 0.01,
            "parseable_as": "float",
            "parseable_rate": 0.98,  # 98% of values parse as float
            "sample_values": ["100.50", "200.00", "invalid", "350.75"]
        }
    }
}
```

### Step-by-Step Execution

**Step 1: Detect castable column**

The profile indicates `dtype="object"` (string) but `parseable_as="float"` with `parseable_rate=0.98`.

Check threshold:
```python
_CAST_PARSEABLE_THRESHOLD = 0.95
parseable_rate = 0.98
0.98 > 0.95  → generate cast step
```

**Step 2: Assess confidence**

```python
_LOW_CONF_CAST_THRESHOLD = 0.99
parseable_rate = 0.98
0.98 < 0.99  → confidence is HIGH but risk flagged (2% data loss)
```

**Step 3: Generate the step**

```python
step = {
    "order": 3,  # casts come after standardize and trim
    "action": "cast",
    "column": "amount",
    "target_type": "float",
    "confidence": 0.9,
    "code_spark": '.withColumn("amount", col("amount").cast("double"))',
    "code_pandas": '["amount"] = pd.to_numeric(df["amount"], errors="coerce")',
    "reason": "98% of values parse as numeric",
    "data_loss_estimate": "~2% of values (20 rows) will become NULL"
}
```

**Step 4: Flag risk**

Since `parseable_rate < _LOW_CONF_CAST_THRESHOLD`:

```python
risks.append(
    "Cast 'amount' to float: ~2% data loss expected. "
    "Review non-parseable values before applying."
)
```

### Output

```python
{
    "kind": "transform_plan",
    "metrics": {
        "total_steps": 1,
        "estimated_data_loss": {"amount": "2%"}
    },
    "steps": [step],
    "findings": [
        "Column 'amount' is text but 98% parseable as float — cast recommended",
        "Estimated 2% data loss on cast (20 rows)"
    ],
    "risks": [
        "Cast 'amount' to float: ~2% data loss expected. Review non-parseable values."
    ]
}
```

---

## Python Patterns

- **Confidence thresholds** — `_HIGH_CONFIDENCE=0.9`, `_MEDIUM_CONFIDENCE=0.7`, `_LOW_CONFIDENCE=0.55`; steps below `_LOW_CONFIDENCE` are excluded from the plan entirely
- **`_STRFTIME_TO_SPARK` mapping** — converts Python strftime format strings to Spark `to_date`/`to_timestamp` format strings for code generation
- **No DataFrame dependency** — operates purely on profile dicts; this separation means the plan can be reviewed, modified, or serialized before any data is touched
- **Dual code generation** — every step includes both `code_spark` and `code_pandas` fields, enabling the executor to choose the appropriate engine
- **Ordered step execution** — steps have an `order` field enforcing: standardize (1) → trim (2) → cast (3) → dedup (4) → validate (5); this prevents dependencies between steps from causing errors
- **Data loss estimation** — for cast operations, estimates how many rows will become NULL based on `parseable_rate`, surfacing this prominently in risks
