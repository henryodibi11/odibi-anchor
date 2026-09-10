# Migration planning

> Preserved migration planning technique.

Evidence-gathering checklist for migration and upgrade tasks. Execute this checklist BEFORE
calling `anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this local reference when migration planning is material to the managed work item.

## Core Principle

Migration is NOT refactoring. Refactoring preserves behavior while changing structure.
Migration changes the underlying API/library/pattern — behavior may change, interfaces
definitely change. The risk is higher because you're changing the contract, not just the wiring.

## Evidence Checklist — Execute In Order

### 1. Map the Old → New Surface

Before touching any code, build the complete translation table:

```python
# Search for migration guides
web_search("library_name migration guide v1 to v2")
web_search("library_name breaking changes changelog")
web_search("library_name deprecated API replacement")
```

**Build the migration map:**

| Old API | New API | Change type | Notes |
|---|---|---|---|
| `old_client.fetch(name)` | `new_client.get(resource=name)` | Signature change | Different params |
| `old_client.save(value)` | `new_client.put(resource, value)` | Pattern change | Caller restructure |
| `CAST(col AS INT)` | `TRY_CAST(NULLIF(TRIM(col), '') AS INT)` | Safety upgrade | Wraps with null handling |
| `from old.module import X` | `from new.module import X` | Import path change | Same functionality |
| `config["key"]` | `config.get("key", default)` | Safety upgrade | Adds default handling |

**Record as known_facts:**
- Complete old → new mapping table
- Which changes are mechanical (find-and-replace) vs. structural (requires rewrite)
- Which old APIs have no direct equivalent (need new approach)
- Which new APIs have no old equivalent (new capabilities to consider)

### 2. Inventory All Usage Sites

```python
anchor("map")  # Full codebase structure
# Then grep for each old API pattern
```

**Record as known_facts:**
- Every file that uses the old API (with line numbers)
- Usage count per file (1 usage vs 50 usages = different risk)
- Which files have tests (safe to migrate) vs. which don't (risky)
- Dependency order (which files must be migrated first)

### 3. Identify Breaking Changes

```python
# Read the changelog/migration guide
web_search("library_name v2 breaking changes")
read_web_page("URL_of_migration_guide")
```

**Record as known_facts:**
- Every breaking change that affects your code
- Behavioral changes (same API, different result)
- Removed APIs (no replacement, need alternative)
- Default value changes (API looks the same but behaves differently)

**Record as risks:**
- Silent behavioral changes (most dangerous — code runs but produces wrong results)
- Performance changes (new version may be faster or slower for your use case)
- Dependency chain effects (upgrading X requires upgrading Y)

### 4. Design Migration Order

**Rule:** Never migrate everything at once. Design an incremental path where each step
leaves the codebase in a working, testable state.

**Migration strategies:**

| Strategy | When to use | Example |
|---|---|---|
| **Strangler fig** | Old and new can coexist | Add new import alongside old, migrate callers one by one, remove old |
| **Big bang** | Old and new are incompatible | Branch, migrate all at once, heavy testing, merge |
| **Adapter** | Need gradual transition | Write adapter that translates old calls to new, migrate behind adapter |
| **File-by-file** | Independent modules | Migrate one file at a time, test between each |

**Record as constraints:**
- Migration strategy chosen and why
- Order of files to migrate (dependency-aware)
- Rollback plan if migration breaks things
- Coexistence rules (can old and new code run side by side?)

### 5. Plan Verification Between Steps

For each migration step, define how to verify it worked:

| Step | Migrate | Verify | Rollback if fails |
|---|---|---|---|
| 1 | `src/client.py` — read path | Existing tests pass + manual read test | Revert the file |
| 2 | `src/client.py` — write path | Write test + output match | Revert the file |
| 3 | `notebooks/pipeline.py` | End-to-end pipeline produces same output | Revert notebook |

**Record as acceptance_criteria:**
- One verification per migration step
- End-to-end test after all steps complete
- Output comparison (old vs new produces same result)
- Performance comparison (new is not significantly slower)

### 6. Bound Historical Context

**Record as known_facts:**
- Current authoritative records describing prior migration issues
- Current compatibility contracts and verified version-specific constraints

After task acceptance, review only its bounded `memory_context` for advisory prior migration
patterns. Do not issue a pre-task full-store/tag scan or force selected advice into the plan.

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Migration: legacy client API → replacement client API",
    "Usage sites: 14 files use the old read API, 11 use the old write API",
    "Breaking changes: replacement writes require an explicit resource name",
    "Breaking changes: replacement reads return values directly, with no .load() chain",
    "Coexistence: old and new CAN coexist — strangler fig strategy",
    "Migration order: utils/ first (leaf nodes), then pipelines (callers)",
    "Files with tests: 8/14 (safe), 6/14 without tests (need tests first or extra caution)",
    "New capability: replacement client has built-in validation — adopt",
    "Past learning: the previous major upgrade changed a shared helper signature",
]

constraints = [
    "Strangler fig: old and new imports coexist during migration",
    "One file per anchor('checkpoint') — verify between each",
    "Write tests for untested files BEFORE migrating them",
    "Do NOT change business logic during migration — structure only",
    "Keep old imports as comments for 1 session after migration (reference)",
]

acceptance_criteria = [
    "Zero legacy client calls remaining in codebase",
    "All existing tests pass after migration",
    "Pipeline output matches pre-migration output (row count, schema, values)",
    "No performance regression >10%",
    "All replacement client calls use the documented signatures",
]

in_scope = ["Migrate all legacy client calls to the replacement API"]
out_of_scope = ["Upgrading unrelated dependencies", "Adding new features during migration"]

risks = [
    "6 files without tests — regression invisible until production",
    "Replacement write behavior may differ from the legacy client — verify",
    "Mitigation: manual output comparison for untested files",
]
```

Continue the accepted work item using the gathered evidence.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.
