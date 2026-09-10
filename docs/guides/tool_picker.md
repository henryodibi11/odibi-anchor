# Tool Picker — Which tool do I use?

| You want to... | Tool | Key output |
|---|---|---|
| **Planning** | | |
| Plan and structure a task with readiness scoring | `task_execution_context()` | readiness score + dimensions |
| Get a quick one-paragraph brief | `quick_context()` | human-readable brief string |
| Hand off state between threads/sessions | `handoff_context()` | handoff packet with next_action |
| **Codebase — Orientation** | | |
| Understand a codebase structure | `codebase_map_context(root)` | package tree + function index |
| Deep-dive a single file | `codebase_map_context(root, focus_file=...)` | classes + functions + imports |
| Find which framework function to use | `framework_lookup_context(query)` | matched functions + signatures |
| **Codebase — Impact & Testing** | | |
| Know what depends on code you're changing | `change_impact_context(root, target=..., function=...)` | risk_level + checklist |
| Find which tests to run after a change | `test_focus_context(root, changed_files=[...])` | run_command + test IDs |
| Enforce naming/structure conventions | `consistency_check_context(root)` | violations + fixes |
| Know conventions before building new code | `convention_preflight_context(root, action=...)` | checklist + naming template |
| Check what obligations you still owe | `workflow_gate_context(root, actions_taken=[...])` | unpaid obligations list |
| **Codebase — Editing** | | |
| Make an intent-based code edit (rename, add param) | `semantic_edit_context(root, target=..., action=...)` | diff_preview + applied flag |
| Lint + type-check files after editing | `preflight_context(root, changed_files=[...])` | error_count + is_safe |
| Find the correct import path for a symbol | `import_resolve_context(root, symbol=...)` | import_statement + candidates |
| Run the full edit→verify→test pipeline | `safe_change_context(root, target=..., action=...)` | is_safe + run_command |
| Check if a proposed edit has failed before | `known_bad_change_context(root, changed_files=[...], action=...)` | status: ok/warn/block |
| **Codebase — Memory & Learning** | | |
| Recall relevant project knowledge | `memory_context(root, files_involved=[...])` | ranked entries list |
| Record a new gotcha or decision | `append_memory(root, entry_type=..., content=...)` | new entry with ID |
| Record that task-selected memory helped | task disposition/application plus evidence-backed evaluation | candidate remains advisory |
| Promote/confirm a memory | Unavailable in this release | candidate remains advisory; no trust increase |
| Mark a memory as false positive | `reject_memory(root, entry_id)` | updated entry |
| Capture and assess task learning | `anchor("learning", "capture|assess", ...)` | evidence-backed observation or truthful no-learning assessment |
| **Codebase — Session** | | |
| Save session state for continuity | `session_snapshot_context(root, decisions=[...])` | snapshot dict |
| Track output regressions across versions | `dogfood_regression_context(current_output, ...)` | drift_detected + deltas |
| **Data — Profiling** | | |
| First look at an unknown table | `exploration_context(df, subject=...)` | grain + freshness + risks |
| Deep statistical profile of columns | `dataset_profile_context(df, subject=...)` | per-column stats + outliers |
| **Data — Validation** | | |
| Pre-write quality gate (nulls, dupes, schema) | `quality_gate_context(df, keys=[...])` | status: passed/blocked |
| Run custom validation rules | `validation_summary_context(df, rules=[...])` | is_safe + per-rule results |
| Check for duplicate keys (grain safety) | `duplicate_key_context(df, keys=[...])` | duplicate_key_count + samples |
| **Data — Comparison** | | |
| Compare two DataFrames row-by-row | `diff_tables_by_key(old_df, new_df, keys=[...])` | added/removed/changed counts |
| Compare schemas between two DataFrames | `schema_diff_context(old_df, new_df)` | added/removed/changed columns |
| Document a table's grain and contract | `table_contract_summary(df, subject=...)` | grain + key + column types |
| **Debugging** | | |
| Parse and structure an error traceback | `error_trace_context(error_text)` | root_cause + blamed_frame |
| Match an error against known failure patterns | `failure_pattern_context(error_text, ...)` | matched fix + never_try list |

---

## anchor() Tool Registry — Registered Tool Actions

These tools are available via the `anchor()` dispatcher after running bootstrap. Each has a README in `tools/<tool_name>/README.md`.

| You want to... | anchor() call | Key output |
|---|---|---|
| **Profiling & Exploration** | | |
| Statistical profile of a DataFrame | `anchor("profile_table", df)` | quality score + per-column stats |
| Deep column histogram + pattern breakdown | `anchor("microscope", df, "col")` | value distribution + patterns |
| Isolate rows with nulls, outliers, or filter | `anchor("case_file", df, column="col", filter="nulls")` | filtered rows + co-occurrence stats |
| Auto-generate validation rules from data | `anchor("suggest_rules", df=df)` | rule list ready for anchor("validate") |
| **Join Safety** | | |
| Validate join safety before a left join | `anchor("pre_join", left_df, right_df, keys=["id"])` | cardinality + null key warnings |
| Validate source safety before MERGE INTO | `anchor("pre_merge", source_df, target, keys=["id"])` | BLOCKED/SAFE + dup key count |
| **Empty Result Diagnosis** | | |
| Find why a DataFrame has 0 rows | `anchor("diagnose_empty", result_df, upstreams={...}, keys=[...])` | dropout_cause + dropout_stage |
| **Row Lineage** | | |
| Trace where a specific row's values came from | `anchor("explain_row", output_df, keys=["id"], values={"id": 1}, upstream={...})` | column_lineage + match_type |
| **Coercion & Fixes** | | |
| Classify value mismatches (case, whitespace, type) | `anchor("coerce_check", source_df, ref_df, keys=["id"])` | coerce_changes per column |
| Apply TRIM/UPPER fixes from coerce_check | `anchor("coerce_fix", df, coerce_ctx)` | corrected df + fixes_applied |
| **Schema Evolution** | | |
| Compare schemas between two DataFrames | `anchor("schema_diff", old_df, new_df)` | added/removed/type_changed columns |
| Generate DDL + PySpark to evolve a table | `anchor("schema_migrate", schema_diff, "catalog.schema.table")` | migration_plan + DDL statements |
| **Delta & Versioning** | | |
| Compare two versions of a Delta table | `anchor("delta_diff", "catalog.schema.table", ["key_col"])` | added/removed/changed row counts |
| **Pipeline Monitoring** | | |
| Check incremental load staleness | `anchor("watermark", source, target, watermark_col="ts")` | lag_hours + pending_count |
| Diagnose small files and partition skew | `anchor("partition_check", "catalog.schema.table")` | small_file_count + OPTIMIZE estimate |

See [Workflows Guide](workflows.md) for multi-step investigation chains.
