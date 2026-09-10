# Safe file editing

> Universal development reference; it is not a discoverable skill.

How to edit files safely using odibi_anchor. Covers tool selection, post-edit obligations, and file-type workflows.

Use alongside Odibi Anchor action documentation when detailed editing mechanics are needed.

## 1. Choosing the Right Edit Tool

### .py files — prefer `anchor("safe")` by default

`anchor("safe")` = full chain (known_bad → semantic_edit → touched → preflight). Use this unless you need manual control.

| Intent | Call |
|---|---|
| Replace a function body | `anchor("safe", target="file.py", action="replace_function", function="func_name", body="...")` |
| Replace a class method | `anchor("safe", target="file.py", action="replace_function", function="func_name", class_name="ClassName", body="...")` |
| Add a new function (module-level) | `anchor("safe", target="file.py", action="add_function", function="name", body="...")` |
| Add a method to a class | `anchor("safe", target="file.py", action="add_function", function="name", class_name="Cls", params="self, x", body="...")` |

### .py files — `anchor("semantic")` for standalone AST ops

`anchor("semantic")` = just the AST edit, no compliance chain. Use when you'll chain manually afterward.

| Intent | Call |
|---|---|
| Add/remove an import | `anchor("semantic", target="file.py", action="add_import", import_statement="from x import y")` |
| Add/remove a decorator | `anchor("semantic", target="file.py", action="add_decorator", function="fn", decorator="property")` |
| Rename function/parameter | `anchor("semantic", target="file.py", action="rename_function", function="old", new_name="new")` |
| Change return type | `anchor("semantic", target="file.py", action="change_return_type", function="fn", return_type="dict")` |
| Add/remove parameter | `anchor("semantic", target="file.py", action="add_parameter", function="fn", param_name="x", param_type="int")` |

### .py files — `executeCode` for complex edits

For body edits (inject lines, add elif, modify return, append to list) or bulk/regex across many files:

```
anchor("known_bad", changed_files=[...])  → before editing
executeCode with file I/O             → make the changes
anchor("touched", "path")                 → immediately after EACH file write
check syntax_check result             → if FAILED, fix before continuing
anchor("preflight")                       → before moving to next task
```

### Non-.py files

| File type | Edit with | Post-edit |
|---|---|---|
| Notebook cells | `editAsset(type="notebook")` | `anchor("touched", "path")` |
| Markdown/config | `editAsset(type="file")` | `anchor("touched", "path")` |

## 2. Post-Edit Protocol (ALL file types)

**Immediately after every file edit**, run:

```python
anchor("touched", "path/to/file.ext")
```

This is mandatory for ALL file types — `.py`, `.md`, `.json`, `.toml`, everything.

| File type | `syntax_check` result | Action on FAILED |
|---|---|---|
| `.py` | `"passed"` or `"FAILED"` | Fix the syntax error NOW — do not continue |
| `.md`, `.json`, `.toml` | `"n/a"` | No check needed, but file IS tracked |

**Why this matters:** Gate sees ALL touched files regardless of extension. Skipping `.md` edits creates tracking gaps.

## 3. Self-Editing Exception

When modifying `anchor("safe")`, `anchor("semantic")`, or `semantic_edit_context.py` itself:
- `executeCode` with raw file I/O is the ONLY option (can't use the tool to edit itself)
- Still MUST chain: `anchor("touched")` → check `syntax_check` → `anchor("preflight")` → `anchor("gate")`
- State the justification explicitly when using this exception

## 4. Unfamiliar Actions — Look Up First

Before calling a `anchor()` action or function you haven't used this session:

```python
anchor("lookup", "function_or_action_name")
```

Cost of lookup: ~100ms. Cost of guessing wrong: full iteration cycle + test failure.
Especially important for: `anchor("safe")` action names, `anchor("semantic")` action names, unfamiliar framework functions.

## 5. Scratch Convention

Files in `_scratch/` directories are free to create and modify without permission gates.
Use these for exploration, prototyping, and throwaway work. When code is ready, promote to `src/` via `anchor("safe")`.

## 6. Edit-Verify Loop (MANDATORY)

NEVER batch edits without verification. The rule:

```
Edit ONE thing → Verify it worked → anchor("touched") → THEN edit the next thing
```

### What counts as verification:

| File type | Verification | Cost |
|---|---|---|
| `.py` | `anchor("touched")` → check `syntax_check` result | Instant |
| `.py` (logic) | Read function back — does it do what you intended? | 3 seconds |
| `.md`/`.json` | Read changed section back | 3 seconds |
| Multi-file | Edit A → verify → touch, Edit B → verify → touch, THEN test | Sequential |

### Expected output (`anchor("touched")`):

```python
result = anchor("touched", "src/transformers/dedup.py")
```
```
# Touched: src/transformers/dedup.py
## Metrics
  - syntax_check: passed
  - file_exists: true
  - tracked_this_session: true
## Findings
  - File registered for gate tracking
```

### Multi-step edit order (safest):

1. **Leaf changes first** (no dependents) → edit → verify → touched
2. **Middle layer** (uses leaf) → edit → verify → touched
3. **Top layer** (entry point) → edit → verify → touched
4. **Tests last** → edit → run → touched

### What NOT to Do:

| Anti-pattern | Why | Do instead |
|---|---|---|
| "I'll verify all at once at the end" | Errors compound — edit #1 breaks #2-#7 | Verify after each edit |
| "This edit is trivial, no need" | Trivial edits have trivial verification cost | Do it anyway |
| Edit 3 files then run tests | If file A was wrong, B and C are wasted | Edit 1 file → verify → repeat |
| "I know this is correct" | You don't until you verify | Read it back (3-second rule) |
| NEVER batch unverified edits across files | Cascading failures cost 10x more to fix | Sequential verification |
| NEVER skip `anchor("touched")` after edit | Gate can't track unregistered files | Always touch after edit |

## 7. Expected Output: `anchor("safe")`

```python
result = anchor("safe", target="src/utils/hash.py", action="append", content="\ndef hash_row(...):\n    ...")
```
```
# Safe Edit: src/utils/hash.py
## Metrics
  - syntax_check: passed
  - backup_created: true
  - lines_added: 5
  - known_bad_verified: true
## Findings
  - Content appended successfully
  - Pre-edit snapshot stored for rollback
## Suggested Next Actions
  - MUST: Run anchor("preflight") to verify imports
  - SHOULD: Run anchor("test") for affected tests
```

## 8. Expected Output: `anchor("preflight")`

```python
result = anchor("preflight")
```
```
# Preflight: PASSED
## Metrics
  - files_checked: 2
  - issues_found: 0
## Findings
  - All changed .py files pass syntax and import checks
```
