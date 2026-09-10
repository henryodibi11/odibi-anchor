# Verify Every Edit

> Lifecycle detail supporting the global operating contract; it is not a discoverable skill.

## When to load
- You are making more than one file edit in a row.
- You catch yourself thinking "I'll verify everything at the end."
- Composes with [anti-rationalization](anti-rationalization.md),
  [compliance gates](compliance-gates.md), and [self-review](self-review.md).

## When NOT to load
- Single one-line edit you are about to gate immediately.
- Read-only investigation (no edits).

## Enforcement
Self-enforced discipline, backed by structural gates: the ungated-edit limit
(`should_block_edit_limit`, max 12) and the checkpoint threshold (10 files)
will block you if you batch edits without verifying. This reference helps keep you
under those limits by design.

## The rule
After **every** file edit, before the next one:

1. `anchor("touched", "<path>")` — registers the change, runs a syntax check on
   `.py` files, and surfaces the per-file memory/test/review hints.
2. If `touched` reports `syntax_check: FAILED`, fix it **now** — do not stack
   another edit on top of broken syntax.
3. For `.py` edits, run `anchor("preflight", changed_files=["<path>"])` before the
   batch grows. Preflight is required at gate (hard rule #11) — running it
   incrementally means no surprise wall of violations at the end.

## Why edit-as-you-go beats verify-at-the-end
If edit #2 introduces a bad assumption, edits #3–#7 built on it are wasted work.
Catching the break at edit #2 saves the cascade. Batched verification also
produces a large, hard-to-attribute diff when something fails.

## Relationship to other skills
- [Safe file editing](../development/file-editing.md) — how to choose `anchor("safe")` vs `anchor("semantic")` for the edit itself.
- [Self-review](self-review.md) — catches **cross-edit** errors before `anchor("gate")`.
- [Compliance gates](compliance-gates.md) — the gate/checkpoint protocol this feeds into.

## Loop
```
edit → anchor("touched", path) → (syntax ok?) → anchor("preflight") → next edit
                                  │ no
                                  └─→ fix before continuing
```
