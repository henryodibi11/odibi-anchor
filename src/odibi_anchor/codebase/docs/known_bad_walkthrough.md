# known_bad_change_context — Code Walkthrough

## Purpose

`known_bad_change_context` is the pre-edit guardrail. Before applying a proposed change,
the agent calls this to check whether the change resembles past failures stored in memory
(gotchas and failure patterns) and whether it introduces any static anti-patterns.

It returns a `status` field of `"ok"`, `"warn"`, or `"block"` that determines whether the
edit should proceed, proceed with caution, or be blocked.

It is invoked by the dispatcher as `anchor("known_bad", changed_files=[...])` and is also
auto-run in the `anchor("safe")` chain before any `.py` file edit.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `known_bad_change_context` | `(root, *, proposed_diff, changed_files, action, task_type, error_text, memory_path, patterns_path, subject, output_format, db_path, project)` | Main builder |
| `render_known_bad_change_report` | `(ctx: dict) -> str` | Renders context dict as markdown |

## Static Anti-Pattern Rules

25 rules in `_STATIC_ANTI_PATTERNS` across 11 categories:

| Category | Example rule IDs |
| --- | --- |
| performance | `collect_large`, `to_pandas_unbounded`, `count_for_emptiness`, `rdd_usage`, `udf_without_pandas` |
| correctness | `cast_without_try`, `bare_except`, `mutable_default_arg`, `no_null_handling` |
| migration | `dbfs_mount_path` |
| reliability | `time_sleep_production`, `cache_persist_unmanaged` |
| security | `hardcoded_credentials`, `string_concat_sql` |
| portability | `display_in_module` |
| compatibility | `spark_context_direct` |
| data_safety | `overwrite_no_condition`, `crossjoin_implicit` |
| maintainability | `import_star`, `schema_star_select`, `todo_fixme_hack`, `global_state_mutation` |
| observability | `print_instead_of_log` |

## Main Execution Flow

```
known_bad_change_context(root, proposed_diff="...", changed_files=["lib/foo.py"],
                         action="rename_parameter")
|
|-- 1. validate_output_format(output_format)
|-- 2. _extract_diff_tokens(proposed_diff) -> list of tokens for scoring
|-- 3. Query memory via memory_context (entry_type="gotcha", limit=20)
|       files_involved=changed_files, tags=[action, task_type]
|-- 4. Query memory via memory_context (entry_type="failure_pattern", limit=20)
|-- 5. Optionally: failure_pattern_context(error_text or diff_tokens) [if patterns_path]
|-- 6. Static anti-pattern scan on proposed_diff (or changed_files if no diff)
|       _check_static_patterns(proposed_diff, file_path=changed_files[0], root=root)
|       strips unified diff prefix (+/-/space) from each line before matching
|       skips time_sleep_production for test files
|       loads .anchor_config.json suppress config
|       -> static_matches list
|-- 7. Score each memory entry against proposed change
|       _score_against_change(entry, changed_files, action, diff_tokens)
|       filter: score >= 1.0 (recency alone doesn't trigger)
|       sort by score descending
|-- 8. Classify status:
|       blocking = entries where confidence >= 0.8 AND confirmation_count >= 2
|       warnings = scored_memories not in blocking
|       status = "block" if blocking else "warn" if (warnings or static_matches) else "ok"
`-- 9. Build output ctx with metrics.status, findings, risks, suggested_next_actions
```

## Suppression Configuration

Suppression is read from `.anchor_config.json` at project root:

```json
{
  "anti_patterns": {
    "suppress_categories": ["maintainability"],
    "suppress_ids": ["print_instead_of_log"],
    "file_overrides": {
      "tests/**": {"suppress_categories": ["performance"]},
      "scripts/*": {"suppress_ids": ["todo_fixme_hack"]}
    }
  }
}
```

Precedence (highest to lowest):
1. Explicit `suppress_categories`/`suppress_ids` kwargs to `known_bad_change_context`
2. File-level overrides from `.anchor_config.json` (matched by `fnmatch`)
3. Project-level suppression from `.anchor_config.json`

## Output Contract

```python
{
    "kind": "known_bad_change_context",
    "subject": str,
    "summary": str,
    "metrics": {
        "status": str,               # "ok" | "warn" | "block"
        "memories_checked": int,
        "patterns_checked": int,
        "memories_matched": int,
        "patterns_matched": int,
        "static_patterns_matched": int,   # builder-specific
        "max_similarity_score": float,
        "blocking_count": int,
        "warning_count": int,
    },
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
    # Not part of base contract but accessible in dict mode:
    # scored_memories, static_matches, matched_patterns
}
```

`metrics.status` is the most important field — the caller checks it to decide whether to
proceed with the edit.

## Worked Examples

### Example 1 — SELECT * Detection (Static Pattern)

```
Input:
  proposed_diff = """
  -    query = "SELECT id FROM orders"
  +    query = "SELECT * FROM orders"
  """
  changed_files = ["pipeline/query_builder.py"]

Execution:
  _check_static_patterns(proposed_diff, file_path="pipeline/query_builder.py", root=root)
  For rule "schema_star_select":
    pattern = r"select(['"]\*['"])|SELECT\s+\*\s+FROM"
    strip diff prefix: "+    query = \"SELECT * FROM orders\""
      -> clean_line = "    query = \"SELECT * FROM orders\""
    pattern.search(clean_line) -> match
    -> static_matches = [{
         "id": "schema_star_select",
         "message": "SELECT * is fragile ...",
         "severity": "warn",
         "category": "maintainability",
         "matched_line": "+    query = \"SELECT * FROM orders\"",
       }]
  status = "warn" (static_matches present, no blocking memories)

Output ctx:
  metrics.status = "warn"
  metrics.static_patterns_matched = 1
  findings = ["WARNING: 1 static pattern(s) flagged.", "  [schema_star_select] SELECT * is fragile..."]
```

### Example 2 — Bare Except Detection

```
Input:
  proposed_diff = """
  +    except:
  +        pass
  """
  changed_files = ["lib/reader.py"]

Execution:
  rule "bare_except": pattern = r"except\s*:"
  strip "+": "    except:" -> pattern matches
  -> static_matches = [{
       "id": "bare_except",
       "message": "Bare except catches SystemExit/KeyboardInterrupt — use except Exception:",
       "severity": "warn",
       "category": "correctness",
     }]
  No memory entries matched (new project, no gotchas yet)
  status = "warn"

  If this same pattern had been caught and saved as a gotcha with confidence=0.9
  and confirmation_count=3 (from 3 previous confirms), status would be "block".
```

## Notable Implementation Patterns

- **Diff prefix stripping**: lines starting with `+`, `-`, or space have the first
  character stripped before pattern matching. This ensures `+    except:` matches
  `r"except\s*:"` correctly without the diff marker interfering.
- **One match per rule**: once a rule fires on any line in the diff, `break` stops scanning
  that rule for additional lines. This prevents a single file with 10 `print()` calls from
  producing 10 findings — only one finding per rule per invocation.
- **Memory scoring threshold `>= 1.0`**: entries where only recency matched (score < 1.0)
  are filtered out. A memory must have file overlap, action match, or token match beyond
  just being recent.
- **Block threshold**: only entries with `confidence >= 0.8` AND `confirmation_count >= 2`
  become blockers. A gotcha that was confirmed once becomes a warning, not a block.
- **Degraded mode**: if `failure_pattern_context` or file scan raises an exception, the
  builder continues with a degraded warning in `matched_patterns` rather than failing the
  entire guardrail call.
- **`time_sleep_production` skipped for test files**: integration tests legitimately use
  `time.sleep()` for polling; the rule is disabled when `"test_"` or `"_test.py"` appears
  in the file path.
