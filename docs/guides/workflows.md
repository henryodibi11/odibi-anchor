# Workflows — Investigation Chains

> Common multi-step investigation patterns using the anchor() tool registry.
> Each chain links tools that hand off naturally — output from one becomes input to the next.

---

## 1. Empty Result Investigation

**Symptom:** Your join or pipeline produced 0 rows and you don't know why.

```python
# Step 1: Profile both inputs to understand their keys
ctx_a = anchor("profile_table", orders_df, subject="orders")
ctx_b = anchor("profile_table", customers_df, subject="customers")

# Step 2: Check join safety before running the join
pre = anchor("pre_join", orders_df, customers_df, keys=["customer_id"])
# → Reveals: NULL keys, cardinality mismatches, key coverage gaps

# Step 3: If join produced empty, diagnose the cause
result_df = orders_df.merge(customers_df, on="customer_id")
ctx = anchor("diagnose_empty", result_df,
         upstreams={"orders": orders_df, "customers": customers_df},
         keys=["customer_id"])
# → ctx["metrics"]["dropout_cause"]: "zero_key_overlap" | "null_keys" | "filter_too_tight"
```

**Tools:** `profile_table` → `pre_join` → `diagnose_empty`

---

## 2. Data Quality Check

**Symptom:** New data arrived and you want to establish validation rules and run them.

```python
# Step 1: Profile to understand data shape
profile = anchor("profile_table", df, subject="incoming_orders", output_format="dict")

# Step 2: Generate rules from profile (no need to write them manually)
rules_ctx = anchor("suggest_rules", profile_ctx=profile)
# → rules_ctx["rules"]: list of not_null, unique, accepted_values, range rules

# Step 3: Run validation
anchor("validate", df, rules=rules_ctx["rules"])

# Optional: inspect a suspicious column before locking in a rule
anchor("microscope", df, "amount")
anchor("case_file", df, column="amount", filter="outliers")
```

**Tools:** `profile_table` → `suggest_rules` → `validate` → `microscope` → `case_file`

---

## 3. Pre-Merge Safety Chain

**Symptom:** You're about to run a MERGE INTO and want to ensure it won't corrupt data.

```python
# Step 1: Check for case/whitespace mismatches that would break the merge key
coerce_ctx = anchor("coerce_check", source_df, target_df, keys=["order_id"])

# Step 2: Fix representation issues
if coerce_ctx["metrics"].get("coerce_pct", 0) > 0:
    fix_ctx = anchor("coerce_fix", source_df, coerce_ctx)
    source_df = fix_ctx["df"]  # use the clean version

# Step 3: Check schema compatibility and duplicate keys
merge_check = anchor("pre_merge", source_df, "catalog.schema.target_table", keys=["order_id"])
# → "BLOCKED" or "SAFE"

# Step 4: After merge, verify what actually changed
delta_ctx = anchor("delta_diff", "catalog.schema.target_table",
               keys=["order_id"], versions_ago=1)
```

**Tools:** `coerce_check` → `coerce_fix` → `pre_merge` → `delta_diff`

---

## 4. Pipeline Staleness Audit

**Symptom:** Downstream dashboards look stale — you need to know if the pipeline is behind.

```python
# Step 1: Check if target is behind source
ctx = anchor("watermark",
         "catalog.schema.source_events",
         "catalog.schema.target_events",
         watermark_col="event_time")
# → ctx["metrics"]["lag_days"], ctx["metrics"]["pending_count"]

# Step 2: If a catch-up load just ran, verify the right rows landed
delta_ctx = anchor("delta_diff", "catalog.schema.target_events",
               keys=["event_id"], versions_ago=1)

# Step 3: Check if the table needs OPTIMIZE after the catch-up load
pc = anchor("partition_check", "catalog.schema.target_events")
```

**Tools:** `watermark` → `delta_diff` → `partition_check`

---

## 5. Schema Change Migration

**Symptom:** Source schema changed and the target Delta table needs to be updated.

```python
# Step 1: Detect schema changes
diff = anchor("schema_diff", old_snapshot_df, new_snapshot_df)
# → diff["added"], diff["removed"], diff["type_changed"]

# Step 2: Generate DDL to evolve the target
ctx = anchor("schema_migrate", diff, "catalog.schema.my_table", dry_run=True)
# → ctx["migration_plan"]["add_columns"][0]["ddl"]

# Step 3: After applying DDL, verify row-level data is unchanged
delta_ctx = anchor("delta_diff", "catalog.schema.my_table",
               keys=["record_id"], versions_ago=1)
# → Should show: added=0, removed=0, changed=0, unchanged=N (schema-only change)
```

**Tools:** `schema_diff` → `schema_migrate` → `delta_diff`

---

## 6. Row Provenance Investigation

**Symptom:** A specific row in your output has a wrong value and you need to trace it back.

```python
# Step 1: Profile the output to identify suspect columns
anchor("profile_table", output_df, subject="enriched_orders")
anchor("microscope", output_df, "revenue")

# Step 2: Isolate the suspect row
ctx = anchor("case_file", output_df, column="revenue", filter="outliers")

# Step 3: Trace the exact column values back to source DataFrames
explain = anchor("explain_row", output_df,
             keys=["order_id"], values={"order_id": 42},
             upstream={"raw_orders": raw_df, "pricing_table": pricing_df})
# → explain["column_lineage"] shows match_type for each column
# → Look for "transformed" entries in explain["risks"]
```

**Tools:** `profile_table` → `microscope` → `case_file` → `explain_row`
