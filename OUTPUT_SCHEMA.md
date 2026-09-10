# OUTPUT_SCHEMA.md

Auto-generated on 2026-07-21 11:05 by `scripts/generate_schema.py`.
**Do not edit manually** — re-run the script to regenerate.

## Standard Contract

Every tool returns a dict with at minimum these keys:

```python
class StandardContract(TypedDict):
    kind: str                        # Tool identifier
    subject: str                     # Human label for the target
    summary: str                     # One-line human summary
    metrics: dict[str, Any]          # Numeric/boolean measures
    findings: list[str | dict]       # Human-readable observations
    risks: list[str]                 # Identified risks
    samples: dict[str, list]         # Evidence rows (empty {} if none)
    suggested_next_actions: list[str] # MUST:/SHOULD: prefixed actions
```

---

## `task_execution_context`

**Signature:** `task_execution_context((task: 'str', *, subject: 'str | None' = None, goal: 'str | None' = None, mode: 'TaskMode' = 'planning', audience: 'Audience' = 'mixed', requester: 'str | None' = None, executor: 'str | None' = None, priority: 'TaskPriority | None' = None, work_type: 'str | None' = None, execution_mode: 'str | None' = None, risk: 'str | None' = None, rigor: 'str | None' = None, domains: 'list[str] | None' = None, traits: 'list[str] | None' = None, caller_required_evidence: 'list[dict[str, Any]] | None' = None, due_date: 'str | None' = None, stakeholders: 'list[str] | None' = None, background: 'str | None' = None, current_state: 'str | None' = None, desired_outcome: 'str | None' = None, trigger: 'str | None' = None, expected_output_format: 'str | None' = None, artifacts: 'list[dict[str, Any]] | None' = None, inputs: 'list[dict[str, Any]] | None' = None, constraints: 'list[str] | None' = None, known_facts: 'list[str] | None' = None, assumptions: 'list[str] | None' = None, open_questions: 'list[str] | None' = None, decisions_needed: 'list[str] | None' = None, evidence: 'list[dict[str, Any]] | None' = None, evidence_gaps: 'list[str] | None' = None, recommended_discovery_steps: 'list[str] | None' = None, options: 'list[dict[str, Any]] | None' = None, in_scope: 'list[str] | None' = None, out_of_scope: 'list[str] | None' = None, dependencies: 'list[str] | None' = None, risks: 'list[str] | None' = None, acceptance_criteria: 'list[str] | None' = None, stop_conditions: 'list[str] | None' = None, guardrails: 'dict[str, Any] | None' = None, deliverables: 'list[str] | None' = None, max_plan_steps: 'int' = 8, max_items_per_section: 'int' = 12, max_text_length: 'int' = 500, include_handoff: 'bool' = True, output_format: 'str' = 'dict', _already_called: "'list[str] | None'" = None) -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `audience` | `str` |  |
| `background` | `dict` |  |
| `constraints` | `list[str]` |  |
| `context` | `dict` |  |
| `critique_checks` | `list[str]` |  |
| `discovery` | `dict` |  |
| `findings` | `list[dict]` | ✓ |
| `guardrails` | `dict` |  |
| `handoff` | `dict` |  |
| `hints` | `dict` |  |
| `intent` | `dict` |  |
| `kind` | `str` | ✓ |
| `metadata` | `dict` |  |
| `metrics` | `dict` | ✓ |
| `mode` | `str` |  |
| `ownership` | `dict` |  |
| `plan` | `list[dict]` |  |
| `readiness` | `dict` |  |
| `required_skills` | `list[dict]` |  |
| `resources` | `dict` |  |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `scope` | `dict` |  |
| `status` | `str` |  |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `task_profile` | `dict` |  |
| `verification` | `dict` |  |
| `version` | `str` |  |

**Metrics keys:**

- `acceptance_criteria_count: int`
- `artifact_count: int`
- `clarifying_question_count: int`
- `constraint_count: int`
- `dependency_count: int`
- `discovery_step_count: int`
- `evidence_count: int`
- `evidence_gap_count: int`
- `guardrail_count: int`
- `input_count: int`
- `open_question_count: int`
- `option_count: int`
- `plan_step_count: int`
- `readiness_score: int`
- `recommended_context_generator_count: int`
- `risk_count: int`
- `stakeholder_count: int`

**Samples:** `{} (empty when no sample data)`

---

## `handoff_context`

**Signature:** `handoff_context((task: 'str', *, state: 'str' = 'in_progress', goal: 'str | None' = None, decisions: 'list[str] | None' = None, rejected_alternatives: 'list[str] | None' = None, blockers: 'list[str] | None' = None, evidence_chain: 'list[dict[str, Any]] | None' = None, artifacts: 'list[dict[str, Any]] | None' = None, next_action: 'str | None' = None, context_needed: 'list[str] | None' = None, skip: 'list[str] | None' = None, open_questions: 'list[str] | None' = None, subject: 'str | None' = None, output_format: 'str' = 'dict') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `artifacts` | `list[Any]` |  |
| `blockers` | `list[Any]` |  |
| `continuation` | `dict` |  |
| `decisions` | `list[Any]` |  |
| `evidence_chain` | `list[Any]` |  |
| `findings` | `list[Any]` | ✓ |
| `goal` | `None` |  |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `open_questions` | `list[Any]` |  |
| `rejected_alternatives` | `list[Any]` |  |
| `risks` | `list[Any]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `state` | `str` |  |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `task` | `str` |  |

**Metrics keys:**

- `artifact_count: int`
- `blocker_count: int`
- `decision_count: int`
- `evidence_step_count: int`
- `is_actionable: bool`
- `is_blocked: bool`
- `open_question_count: int`
- `skip_count: int`

**Samples:** `{} (empty when no sample data)`

---

## `codebase_map_context`

**Signature:** `codebase_map_context((root: 'str | Path', *, subject: 'str | None' = None, include_private: 'bool' = False, include_tests: 'bool' = True, test_dirs: 'list[str] | None' = None, max_files: 'int' = 1000, max_docstring_chars: 'int' = 500, extract_return_keys: 'bool' = True, focus_file: 'str | None' = None, detect_dead_code: 'bool' = True, detail: 'str' = 'full', output_format: 'str' = 'dict', frame: "'Any | None'" = None) -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `dead_code_candidates` | `list[dict]` |  |
| `dependency_graph` | `dict` |  |
| `exports` | `dict` |  |
| `findings` | `list[str]` | ✓ |
| `focus` | `None` |  |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `modules` | `dict` |  |
| `recommended_workflow` | `str` |  |
| `reverse_dependency_graph` | `dict` |  |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `test_map` | `dict` |  |
| `workflow_steps_remaining` | `list[str]` |  |

**Metrics keys:**

- `class_count: int`
- `detail: str`
- `init_file_count: int`
- `module_count: int`
- `parse_error_count: int`
- `private_function_count: int`
- `public_function_count: int`
- `source_file_count: int`
- `source_files_with_tests: int`
- `test_coverage_pct: float`
- `test_file_count: int`
- `testable_source_files: int`
- `total_source_lines: int`

**Samples:** `{} (empty when no sample data)`

---

## `change_impact_context`

**Signature:** `change_impact_context((root: 'str | Path', *, target: 'str', function: 'str | None' = None, change_type: 'str' = 'modify_logic', subject: 'str | None' = None, include_docs: 'bool' = True, doc_dirs: 'list[str] | None' = None, include_infra: 'bool' = True, output_format: 'str' = 'dict') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `call_sites` | `list[dict]` |  |
| `checklist` | `list[str]` |  |
| `doc_references` | `list[dict]` |  |
| `findings` | `list[str]` | ✓ |
| `importers` | `list[dict]` |  |
| `infra_references` | `list[dict]` |  |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `risk_assessment` | `dict` |  |
| `risks` | `list[Any]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `test_files` | `list[dict]` |  |

**Metrics keys:**

- `call_site_count: int`
- `change_type: str`
- `doc_reference_count: int`
- `function: str`
- `importer_count: int`
- `infra_reference_count: int`
- `is_breaking: bool`
- `risk_level: str`
- `target: str`
- `test_file_count: int`

**Samples:** `{} (empty when no sample data)`

---

## `session_snapshot_context`

**Signature:** `session_snapshot_context((root: 'str | Path', *, mode: 'str' = 'full', summary: 'str | None' = None, state: 'str | None' = None, subject: 'str | None' = None, decisions: 'list[str] | None' = None, rejected_alternatives: 'list[str] | None' = None, open_questions: 'list[str] | None' = None, next_steps: 'list[str] | None' = None, session_notes: 'str | None' = None, test_state: 'dict[str, int] | None' = None, previous_snapshot: 'dict[str, Any] | None' = None, conventions: 'dict[str, str] | None' = None, compact: 'bool' = False, output_format: 'str' = 'dict', **kwargs: 'Any') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `changes_since` | `None` |  |
| `conventions` | `dict[str, Any]` |  |
| `decisions` | `list[str]` |  |
| `file_states` | `dict` |  |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `next_steps` | `list[Any]` |  |
| `open_questions` | `list[Any]` |  |
| `rejected_alternatives` | `list[Any]` |  |
| `risks` | `list[Any]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `session_notes` | `None` |  |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `test_state` | `None` |  |
| `timestamp` | `str` |  |

**Metrics keys:**

- `file_count: int`
- `md_file_count: int`
- `py_file_count: int`
- `total_lines: int`
- `total_size_bytes: int`

**Samples:** `{} (empty when no sample data)`

---

## `consistency_check_context`

**Signature:** `consistency_check_context((root: 'str | Path', *, subject: 'str | None' = None, rules: 'list[str] | None' = None, exclude_rules: 'list[str] | None' = None, exclude_paths: 'list[str] | None' = None, custom_rules: 'list[dict[str, Any]] | None' = None, changed_files: 'list[str] | None' = None, scope: 'str' = 'all', suppress_suggestions: 'bool' = False, output_format: 'str' = 'dict') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `compliant` | `list[dict]` |  |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `post_change_suggestions` | `list[Any]` |  |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `violations` | `list[dict]` |  |

**Metrics keys:**

- `compliant_count: int`
- `compliant_truncated: int`
- `files_scanned: int`
- `rules_checked: int`
- `scoped_to_changed: bool`
- `total_violations_found: int`
- `violation_by_rule: dict`
- `violation_count: int`
- `violations_in_scope: int`

**Samples:** `{} (empty when no sample data)`

---

## `convention_preflight_context`

**Signature:** `convention_preflight_context((root: 'str | Path', *, action: 'str' = 'new_function', target_file: 'str | None' = None, function_name: 'str | None' = None, changed_files: 'list[str] | None' = None, subject: 'str | None' = None, output_format: 'str' = 'dict') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `anti_pattern_violations` | `list[Any]` |  |
| `checklist` | `list[dict]` |  |
| `examples` | `dict` |  |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |

**Metrics keys:**

- `action: str`
- `checklist_item_count: int`
- `has_target_file: bool`
- `patterns_scanned: int`

**Samples:** `{} (empty when no sample data)`

---

## `test_focus_context`

**Signature:** `test_focus_context((root: 'str | Path', *, changed_files: 'list[str] | None' = None, changed_functions: 'list[str] | None' = None, subject: 'str | None' = None, test_dir: 'str' = 'tests', src_dir: 'str' = 'src', include_test_ids: 'bool' = False, output_format: 'str' = 'dict') -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `affected_test_files` | `list[dict]` |  |
| `coverage_gaps` | `list[dict]` |  |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `obligations_created` | `list[dict]` |  |
| `obligations_paid` | `list[str]` |  |
| `risks` | `list[str]` | ✓ |
| `run_command` | `str` |  |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `suggested_test_cases` | `list[str]` |  |
| `summary` | `str` | ✓ |
| `targeted_run_command` | `str` |  |
| `targeted_test_ids` | `list[Any]` |  |

**Metrics keys:**

- `affected_test_files_count: int`
- `changed_functions_count: int`
- `coverage_gaps_count: int`
- `estimated_run_seconds: float`
- `targeted_test_ids_count: int`
- `test_functions_in_scope: int`

**Samples:** `{} (empty when no sample data)`

---

## `workflow_gate_context`

**Signature:** `workflow_gate_context((root: 'str | Path', *, actions_taken: 'list[str]', files_changed: 'list[str] | None' = None, files_created: 'list[str] | None' = None, files_read: 'list[str] | None' = None, workflow: 'str | None' = None, obligations_paid: 'list[str] | None' = None, verification_record: 'list[dict[str, str]] | None' = None, skip_timing_verification: 'bool' = False, _timings_override: 'list[dict] | None' = None, subject: 'str | None' = None, output_format: 'str' = 'dict') -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `learn_reminder` | `None` |  |
| `metrics` | `dict` | ✓ |
| `obligations` | `list[dict]` |  |
| `obligations_paid` | `list[Any]` |  |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `verification_record` | `list[Any]` |  |

**Metrics keys:**

- `actions_count: int`
- `all_verified: bool`
- `files_changed_count: int`
- `files_created_count: int`
- `must_unpaid: int`
- `obligations_owed: int`
- `obligations_paid: int`
- `risk_level: str`
- `should_unpaid: int`
- `timing_rejected: list[Any]`
- `timing_verified: list[Any]`
- `verifications_failed: int`
- `verifications_passed: int`
- `verifications_recorded: int`
- `workflow: str`

**Samples:** `{} (empty when no sample data)`

---

## `framework_lookup_context`

**Signature:** `framework_lookup_context((query: 'str', *, framework_root: 'str | None' = None, category: 'str | None' = None, max_results: 'int' = 5, include_source: 'bool' = False, output_format: 'str' = 'dict') -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `matches` | `list[Any]` |  |
| `metrics` | `dict` | ✓ |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |

**Metrics keys:**

- `categories_searched: int`
- `matches_found: int`
- `query: str`
- `registry_size: int`

**Samples:** `{} (empty when no sample data)`

---

## `dogfood_regression_context`

**Signature:** `dogfood_regression_context((current_output: 'dict[str, Any]', *, baseline_dir: 'str' = '.dogfood_baselines', subject: 'str | None' = None, save_as_baseline: 'bool' = False, root: 'str | None' = None, output_format: 'str' = 'dict') -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `baseline_path` | `str` |  |
| `diffs` | `list[Any]` |  |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `risks` | `list[Any]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |

**Metrics keys:**

- `baseline_age_hours: None`
- `improvements_count: int`
- `regressions_count: int`
- `unchanged_count: int`

**Samples:** `{} (empty when no sample data)`

---

## `quality_gate_context`

**Signature:** `quality_gate_context((df: 'Any', *, keys: 'list[str] | None' = None, target_schema: 'Any' = None, checks: 'list[str] | None' = None, subject: 'str' = 'quality_gate', engine: 'Engine' = 'auto', sample_limit: 'int' = 10, output_format: 'OutputFormat' = 'dict', freshness_column: 'str | None' = None, df_name: 'str' = 'df', thresholds: 'dict[str, Any] | None' = None) -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `checks` | `list[dict]` |  |
| `engine` | `str` |  |
| `findings` | `list[dict]` | ✓ |
| `fix_all_expr` | `str` |  |
| `fix_impact` | `dict` |  |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `recommendation` | `str` |  |
| `risks` | `list[dict]` | ✓ |
| `samples` | `dict` | ✓ |
| `status` | `str` |  |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `write_safety_card` | `str` |  |

**Metrics keys:**

- `checks_failed: int`
- `checks_passed: int`
- `checks_run: int`
- `checks_warned: int`
- `is_write_safe: bool`
- `total_rows: int`
- `verdict: str`

**Samples:** `{"completeness": list[dict], "duplicate_keys": list[dict]}`

---

## `validation_summary_context`

**Signature:** `validation_summary_context((df: 'Any', rules: 'list[dict[str, Any]] | None' = None, *, subject: 'str' = 'validation', engine: 'Engine' = 'auto', severity_map: 'dict[str, str] | None' = None, sample_failures: 'int' = 5, output_format: 'OutputFormat' = 'dict', spark: 'Any' = None, from_manifest: 'bool' = False, root: 'str | None' = None, frame: 'Any | None' = None) -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `blockers` | `list[Any]` |  |
| `findings` | `list[Any]` | ✓ |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `quarantine_call` | `str` |  |
| `recommendation` | `str` |  |
| `risks` | `list[Any]` | ✓ |
| `rules` | `list[dict]` |  |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `warnings` | `list[Any]` |  |

**Metrics keys:**

- `blocker_count: int`
- `engine: str`
- `is_promotion_safe: bool`
- `manifest_rules_generated: int`
- `new_rule_types_used: list[Any]`
- `overall_pass_rate: float`
- `rows_with_any_failure: int`
- `rules_evaluated: int`
- `rules_failed: int`
- `rules_passed: int`
- `total_rows: int`
- `warning_count: int`

**Samples:** `{} (empty when no sample data)`

---

## `duplicate_key_context`

**Signature:** `duplicate_key_context((df: 'Any', keys: 'str | Sequence[str]', *, subject: 'str | None' = None, engine: 'str' = 'auto', sample_limit: 'int' = 20, include_duplicate_rows: 'bool' = False, row_sample_limit: 'int' = 20, treat_nulls_as_duplicates: 'bool' = True, output_format: 'str' = 'dict') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `duplicate_pattern` | `dict` |  |
| `findings` | `list[str]` | ✓ |
| `has_duplicates` | `bool` |  |
| `has_null_keys` | `bool` |  |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `passed` | `bool` |  |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |

**Metrics keys:**

- `all_null_key_row_count: int`
- `duplicate_key_count: int`
- `duplicate_key_rate: float`
- `duplicate_row_count: int`
- `duplicate_row_rate: float`
- `engine: str`
- `excess_duplicate_row_count: int`
- `excess_duplicate_row_rate: float`
- `key_column_count: int`
- `key_columns: list[str]`
- `max_rows_per_key: int`
- `null_counts_by_key: dict`
- `null_key_rate: float`
- `null_key_row_count: int`
- `row_count: int`
- `sample_limit: int`
- `sampled_duplicate_key_count: int`
- `treat_nulls_as_duplicates: bool`
- `unique_key_count: int`

**Samples:** `{"duplicate_keys": list[dict], "duplicate_rows": list (empty)}`

---

## `diff_tables_by_key`

**Signature:** `diff_tables_by_key((old_df: Any, new_df: Any, keys: list[str], *, compare_columns: list[str] | None = None, subject: str | None = None, engine: str = 'auto', sample_limit: int = 20, output_format: str = 'dict') -> dict[str, Any] | str)`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `change_pattern` | `dict` |  |
| `columns` | `dict` |  |
| `engine` | `str` |  |
| `findings` | `list[dict]` | ✓ |
| `kind` | `str` | ✓ |
| `merge_expr` | `str` |  |
| `metrics` | `dict` | ✓ |
| `risks` | `list[dict]` | ✓ |
| `samples` | `dict` | ✓ |
| `status` | `str` |  |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |

**Metrics keys:**

- `added_key_count: int`
- `added_key_pct: float`
- `changed_cell_count: int`
- `changed_column_counts: list[dict]`
- `changed_key_count: int`
- `changed_key_pct: float`
- `common_column_count: int`
- `common_key_count: int`
- `compare_dtype_change_count: int`
- `compared_column_count: int`
- `has_changes: bool`
- `key_dtype_change_count: int`
- `new_duplicate_key_count: int`
- `new_duplicate_row_count: int`
- `new_key_count: int`
- `new_null_key_row_count: int`
- `new_only_column_count: int`
- `new_row_count: int`
- `old_duplicate_key_count: int`
- `old_duplicate_row_count: int`
- `old_key_count: int`
- `old_null_key_row_count: int`
- `old_only_column_count: int`
- `old_row_count: int`
- `removed_key_count: int`
- `removed_key_pct: float`
- `row_count_delta: int`
- `type_changed_column_count: int`
- `unchanged_key_count: int`

**Samples:** `{"added_keys": list[dict], "added_rows": list[dict], "changed_rows": list[dict], "new_duplicate_keys": list (empty), "new_null_key_rows": list (empty), "old_duplicate_keys": list (empty), "old_null_key_rows": list (empty), "removed_keys": list[dict], "removed_rows": list[dict]}`

---

## `schema_diff_context`

**Signature:** `schema_diff_context((old_df: 'Any', new_df: 'Any', *, old_subject: 'str' = 'old_df', new_subject: 'str' = 'new_df', subject: 'str | None' = None, engine: 'Engine' = 'auto', include_unchanged: 'bool' = True, max_unchanged: 'int' = 50, output_format: 'OutputFormat' = 'dict', spark: 'Any' = None) -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `added` | `list[Any]` |  |
| `findings` | `list[Any]` | ✓ |
| `kind` | `str` | ✓ |
| `likely_renames` | `list[Any]` |  |
| `metrics` | `dict` | ✓ |
| `new_subject` | `str` |  |
| `nullable_changed` | `list[Any]` |  |
| `old_subject` | `str` |  |
| `removed` | `list[Any]` |  |
| `reordered` | `list[Any]` |  |
| `risks` | `list[Any]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `type_changed` | `list[Any]` |  |
| `unchanged` | `list[dict]` |  |
| `write_safety` | `dict` |  |

**Metrics keys:**

- `added_count: int`
- `compatibility: str`
- `engine: str`
- `has_changes: bool`
- `is_breaking_change: bool`
- `net_column_delta: int`
- `new_column_count: int`
- `nullable_changed_count: int`
- `old_column_count: int`
- `removed_count: int`
- `reordered_count: int`
- `type_changed_count: int`
- `unchanged_count: int`
- `unchanged_returned_count: int`
- `unchanged_truncated_count: int`

**Samples:** `{} (empty when no sample data)`

---

## `table_contract_summary`

**Signature:** `table_contract_summary((df: 'Any', *, subject: 'str' = 'dataframe', candidate_key_columns: 'Sequence[str] | None' = None, freshness_columns: 'Sequence[str] | None' = None, profile_columns: 'Sequence[str] | None' = None, sample_columns: 'Sequence[str] | None' = None, sample_limit: 'int' = 10, max_profile_columns: 'int' = 50, high_null_rate_threshold: 'float' = 0.5, stale_after_days: 'float | None' = None, reference_time: 'str | datetime | pd.Timestamp | None' = None, include_value_examples: 'bool' = True, engine: 'str' = 'auto', spark_sample_size: 'int' = 5000, output_format: 'str' = 'dict') -> 'dict[str, Any] | str')`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `candidate_keys` | `dict` |  |
| `findings` | `list[dict]` | ✓ |
| `freshness` | `dict` |  |
| `kind` | `str` | ✓ |
| `metrics` | `dict` | ✓ |
| `profile` | `dict` |  |
| `risks` | `list[Any]` | ✓ |
| `samples` | `dict` | ✓ |
| `schema` | `list[dict]` |  |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |

**Metrics keys:**

- `all_null_column_count: int`
- `column_count: int`
- `constant_column_count: int`
- `detected_freshness_column_count: int`
- `duplicate_row_count: int`
- `high_null_column_count: int`
- `inferred_single_column_key_count: int`
- `memory_bytes: int`
- `omitted_column_count: int`
- `profile_target_column_count: int`
- `profiled_column_count: int`
- `provided_key_duplicate_row_count: None`
- `provided_key_is_unique: None`
- `provided_key_null_row_count: None`
- `row_count: int`
- `sample_column_count: int`
- `sample_row_count: int`
- `unprofiled_table_column_count: int`

**Samples:** `{"data_preview": list[dict]}`

---

## `error_trace_context`

**Signature:** `error_trace_context((error_text: 'str | BaseException', *, subject: 'str | None' = None, df: 'Any | None' = None, engine: 'str' = 'auto', max_chars: 'int' = 6000, sample_limit: 'int' = 5, metadata: 'Mapping[str, Any] | None' = None, include_dataframe_sample: 'bool' = True, output_format: 'str' = 'dict') -> "'dict[str, Any] | str'")`

**Output keys:**

| Key | Type | Contract |
| --- | --- | --- |
| `dataframe_context` | `None` |  |
| `error` | `dict` |  |
| `findings` | `list[str]` | ✓ |
| `kind` | `str` | ✓ |
| `location` | `dict` |  |
| `metadata` | `dict[str, Any]` |  |
| `metrics` | `dict` | ✓ |
| `relevant_trace` | `str` |  |
| `risks` | `list[str]` | ✓ |
| `samples` | `dict[str, Any]` | ✓ |
| `subject` | `str` | ✓ |
| `suggested_next_actions` | `list[str]` | ✓ |
| `summary` | `str` | ✓ |
| `trace_frames` | `list[Any]` |  |

**Metrics keys:**

- `dataframe_engine: None`
- `exception_chain_count: int`
- `has_dataframe_context: bool`
- `input_char_count: int`
- `input_line_count: int`
- `max_chars: int`
- `relevant_trace_char_count: int`
- `trace_frame_count: int`

**Samples:** `{} (empty when no sample data)`

---
