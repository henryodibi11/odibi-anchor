# Contributing

## Adding a New Context Generator

1. **Create module** in appropriate subpackage:
   - `validation/` — checks that gate a write
   - `tables/` — comparisons between tables
   - `profiling/` — statistical analysis of a single table
   - `planning/` — task structuring (no data deps)
   - `codebase/` — code analysis and workflow gates
   - `debugging/` — error parsing and diagnosis

2. **Follow naming**: `{domain}_{noun}_context.py`

3. **Function signature**:
   ```python
   def my_context(df, *, subject="dataframe", engine="auto", output_format="dict", **kwargs):
       ...
   ```

4. **Output contract** (ALL keys required):
   ```python
   return {
       "kind": "my_context",
       "subject": subject,
       "summary": "One-line human summary.",
       "metrics": {"total_rows": n, ...},
       "findings": ["Human-readable observation", ...],
       "risks": ["Identified risk", ...],
       "samples": {"semantic_key": [row_dicts]} if data else {},
       "suggested_next_actions": ["MUST: ...", "SHOULD: ..."],
   }
   ```

   **samples rules:**
   - Empty = `{}` (never `[]` or `{"key": []}`)
   - Non-empty = `{"semantic_key": [list_of_dicts]}` (e.g., `"data_preview"`, `"duplicate_keys"`)
   - Values are always lists

5. **Engine detection** (if supporting Spark):
   ```python
   from odibi_anchor._utils.engine_utils import detect_engine
   engine = detect_engine(df) if engine == "auto" else engine
   ```

6. **Tests**: Add `tests/{subpackage}/test_my_context.py` with:
   - Happy path (clean data)
   - Edge cases (empty, nulls, single row)
   - Output contract validation
   - JSON serialization check

7. **Docs**: Add `docs/tools/my_context.md`

8. **Exports**: Update `{subpackage}/__init__.py`

9. **Regenerate schema**: Run `python scripts/generate_schema.py`

## Schema Validation

The output contract is enforced by automated tests:

```bash
# Regenerate OUTPUT_SCHEMA.md (run after any tool changes)
python scripts/generate_schema.py

# Run contract validation (222 parametrized tests)
pytest tests/test_output_schema.py -v
```

The schema test validates:
- All 8 standard contract keys present
- `samples` is always `dict` (never list)
- `metrics` is always `dict`
- `findings` and `risks` are always lists
- `suggested_next_actions` items are strings
- `kind` and `summary` are non-empty strings
- When `samples` has keys, values are lists

## Running Tests

```bash
cd odibi_anchor
python -B -m pytest tests/ -p no:cacheprovider --tb=short -q
```

## Code Style

```bash
pip install ruff
ruff check src/ tests/
ruff format src/ tests/
```

## Pytest Configuration

- `testpaths = ["tests"]` — only collect from tests/
- `norecursedirs = ["src", "scripts"]` — never traverse source for test collection
- Source modules named `test_*` (e.g., `test_focus_context.py`) must have `__test__ = False`
