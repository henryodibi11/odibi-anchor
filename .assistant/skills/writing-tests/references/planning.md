# Test-planning reference

> Preserved test planning technique.

Evidence-gathering checklist for test-writing tasks. Execute this checklist BEFORE calling
`anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this reference only after `writing-tests` owns an explicit test-authoring outcome.

## When to Use This Mode

| Scenario | Example |
|---|---|
| Writing tests for new code | "Add tests for the new rollback() function" |
| Expanding coverage for existing code | "We have 0 tests for dispatcher.py — fix that" |
| Writing tests before refactoring | "Cover the current behavior so we can safely restructure" |
| Data validation tests | "Write tests that verify bronze→silver transform logic" |
| Regression tests | "This bug should never happen again — write a test" |

## Evidence Checklist — Execute In Order

### 1. Read the Code Under Test

```python
readFile("src/module_to_test.py")
```

**Record as known_facts:**
- Every public function/method name and its signature (params, types, defaults, return type)
- What each function does (one sentence)
- Dependencies the function needs (imports, other modules, external services)
- Side effects (writes to disk, modifies state, calls external APIs)
- Current line count and complexity

**Rule:** You cannot write tests for code you haven't read.
"I know roughly what it does" → read it. Assumptions about behavior become wrong tests.

### 2. Read Existing Tests

```python
anchor("test", changed_files=["src/module_to_test.py"])
```

Then read the test file if it exists:

```python
readFile("tests/test_module.py")
```

**Record as known_facts:**
- Which functions already have tests
- Which functions have NO tests (these are your targets)
- Test framework and patterns used (pytest fixtures, parametrize, marks)
- How mocking is done in existing tests
- Test file naming convention
- Fixture patterns already established

**If no test file exists:**
- Record: "No existing tests — creating new test file"
- Check neighboring test files for conventions: `readFile("tests/test_similar_module.py")`

### 3. Understand the Test Infrastructure

#### Spark/PySpark Tests

```python
anchor("lookup", "import isolation")  # Find the current project's test-import pattern
```

**Record as known_facts (for data engineering tests):**
- **The `_load()` pattern**: `importlib.util.spec_from_file_location` bypasses the broken `__init__.py` chain. The chain imports ALL subpackages (PySpark, Delta, connectors) which fail without a SparkSession. The helper injects a dummy module and loads files directly.
- Whether tests need a SparkSession fixture or can use pure Python
- Whether tests use real Delta tables or mock DataFrames
- How test data is created (inline dicts → `spark.createDataFrame()`, fixture files, or factories)

#### Pure Python Tests

**Record as known_facts:**
- How dependencies are mocked (unittest.mock, monkeypatch, dependency injection)
- Whether the module uses global state (needs cleanup/reset between tests)
- File I/O patterns (tmp_path fixture, mock filesystem)

### 4. Design Test Cases

For each function under test, design cases using this matrix:

#### Happy Path (minimum 1 per function)

| Case | Input | Expected output | Why this matters |
|---|---|---|---|
| Normal operation | Typical valid input | Expected result | Proves the function works at all |

#### Edge Cases (minimum 2 per function)

| Case | Input | Expected output | Why this matters |
|---|---|---|---|
| Empty input | `[]`, `None`, empty DataFrame | Defined behavior (empty result or error) | Prevents silent corruption on empty data |
| Single item | One row/element | Correct result | Catches off-by-one, aggregation on single row |
| Null values | NULLs in key columns | Defined behavior | Data always has NULLs — test for it |
| Boundary values | Max int, empty string, zero | No crash, correct handling | Catches type assumptions |
| Duplicate input | Same key appears twice | Dedup or defined behavior | Tests idempotency |

#### Error Cases (minimum 1 per function)

| Case | Input | Expected behavior | Why this matters |
|---|---|---|---|
| Wrong type | String where int expected | `TypeError` or graceful handling | Documents the contract |
| Missing required field | Dict without expected key | `KeyError` or clear error | Prevents cryptic failures |
| Invalid state | Called before initialization | Clear error message | Guides debugging |

#### Data Engineering Specific Cases

| Case | Input | Expected | Why |
|---|---|---|---|
| Schema drift | DataFrame missing a column | Clear error, not silent NULL | Upstream changes happen |
| Type coercion | "1,234.5" in numeric column | Correct parse or NULL | Excel sources always have this |
| Date formats | "01/15/2024", "2024-01-15", "Jan 15, 2024" | All parse correctly | ISO sources vary |
| Large values | Capacity = 999,999 MW | No overflow | Catches numeric limits |
| Special characters | Project name with quotes, unicode | No SQL injection or encoding error | Real data has these |

**Record as acceptance_criteria:**
- Number of test cases per function
- Coverage target (which functions, which paths)
- All edge cases from the matrix above that apply

### 5. Plan Test Structure

**File layout:**
```
tests/
├── test_module_name.py          # Unit tests for src/module_name.py
├── test_module_name_integration.py  # Integration tests (if needed)
├── conftest.py                  # Shared fixtures
└── fixtures/                    # Test data files (if needed)
```

**Test naming convention:**
```python
def test_function_name_when_condition_then_expected():
    """Tests that function_name does expected when condition."""
```

Examples:
```python
def test_rollback_when_valid_step_name_then_returns_prior_state():
def test_rollback_when_evicted_checkpoint_then_raises_key_error():
def test_rollback_when_step_zero_then_returns_original_input():
def test_dispatch_when_unknown_action_then_raises_value_error():
```

**Record as constraints:**
- Follow existing test naming conventions in the project
- Use existing fixture patterns (don't invent new ones if similar exist)
- Keep tests independent (no test depends on another test's output)
- Each test tests ONE thing (one assertion per behavior, not per test function)

### 6. Plan Mocking Strategy

| Dependency | Mock strategy | Fixture |
|---|---|---|
| SparkSession | Use `conftest.py` fixture or `_load()` bypass | `@pytest.fixture(scope="session")` |
| File I/O | `tmp_path` fixture (pytest built-in) | Per-test temp directory |
| External API | `unittest.mock.patch` or `monkeypatch` | Mock returns known data |
| Database | In-memory SQLite or mock | Fixture with setup/teardown |
| Time/dates | `freezegun` or `monkeypatch` on `datetime` | Fixed timestamps |
| Global state | Reset in fixture teardown | `@pytest.fixture(autouse=True)` |

**Record as known_facts:**
- Which dependencies need mocking
- Which mock strategy for each
- Whether fixtures already exist for these mocks

### 7. Bound Historical Context

**Record as known_facts:**
- Current test contracts and observed fragile behavior
- Fixture patterns verified in the current suite

After task acceptance, review only its bounded `memory_context` for advisory prior testing
patterns. Do not issue a pre-task full-store/tag scan or force selected advice into the plan.

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Code under test: src/odibi_anchor/tables.py — rollback() function at line 410",
    "Signature: rollback(result: dict, *, to: str | int) -> DataFrame",
    "Dependencies: pyspark DataFrame, result dict from apply_transform_context()",
    "Existing tests: tests/test_tables.py — 5 tests for apply_transform_context, 0 for rollback",
    "Test framework: pytest with conftest.py fixtures",
    "Fixture: spark_session fixture exists in conftest.py (scope=session)",
    "Mock strategy: create result dict with mock checkpoints, no real Spark needed for unit tests",
    "The _load() pattern: use importlib to bypass __init__.py chain for isolated loading",
    "Past learning: unpersist() must check is_cached before calling — test this edge case",
]

constraints = [
    "Follow existing test naming: test_function_when_condition_then_expected",
    "Use existing spark_session fixture from conftest.py",
    "Keep tests independent — no shared state between tests",
    "Each test tests one behavior",
    "Use _load() pattern to import tables.py directly",
]

acceptance_criteria = [
    "rollback() has minimum 5 tests: happy path (by name), happy path (by index), "
    "rollback to start, evicted checkpoint (KeyError), invalid step name (KeyError)",
    "All existing 5 tests still pass",
    "New tests run in <2 seconds (no real Spark operations)",
    "anchor('test') confirms coverage for rollback()",
]

in_scope = ["Unit tests for rollback() function"]
out_of_scope = ["Integration tests with real Spark", "Tests for other functions in tables.py"]

risks = [
    "If result dict structure is complex, mock may diverge from real shape — "
    "read apply_transform_context() to get exact dict structure",
]
```

Continue the accepted task using the gathered evidence.
