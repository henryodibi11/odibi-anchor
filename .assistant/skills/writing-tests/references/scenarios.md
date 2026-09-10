# Real-client test scenarios

> Preserved real-client scenario and isolation technique.

Dogfooding is NOT "run the tool and see if the output looks right."

Dogfooding IS: **read the source → predict the output → run the tool → compare prediction vs reality → classify every discrepancy → preserve session evidence and assess reusable findings.**

The difference matters because agents will run a tool, glance at the output, say "looks reasonable," and move on. That catches nothing. Real dogfooding catches bugs that look reasonable — the output seems fine but is subtly wrong because you never understood what "right" looks like.

## The Non-Negotiable Sequence

Every dogfood pass follows this exact sequence. No exceptions. No reordering. No skipping.

### Step 1: READ SOURCE

Before running the tool, read the source code of the function you're about to test.

You must be able to answer:
- What inputs does it accept?
- What logic does it apply? (thresholds, conditions, branches)
- What output shape does it return?
- What edge cases does the code handle?
- What edge cases does the code NOT handle?

**Proof requirement:** Write down specific, falsifiable behaviors warranted by the source and risk. Not vague:
- ❌ "It should detect format issues" (vague, useless)
- ✅ "Column `date_field` has values '2024-01-15' and '01/15/2024'. The `_check_mixed_date_format()` function uses regex matching with a 5% threshold. Since ~40% of values use MM/DD/YYYY, this should trigger a `mixed_date_format` finding with severity='warning'." (specific, falsifiable)

**If you can't write specific predictions, you haven't read the source well enough. Go back and read more.**

### Step 2: PREDICT

Write down your predictions BEFORE running anything. This is the critical step agents skip.

For each test input, predict:
- What will the `metrics` dict contain? (specific keys and approximate values)
- What `findings` will be generated? (which ones and why)
- What `risks` will be surfaced?
- What will `samples` contain?
- What will NOT appear in the output? (equally important)

Format your predictions explicitly:

```
PREDICTION: profile_column(df, "account_id")
- metrics.null_pct → 0.0 (no nulls visible in source data)
- metrics.distinct_count → ~985 (high cardinality, near-unique)
- metrics.distinct_normalized → ~970 (trailing spaces should reduce distinctness)
- findings → should include "trailing_spaces_detected" (I see spaces in sample values)
- findings → should NOT include "mixed_date_format" (this is a string ID, not a date)
- risks → should include join mismatch risk (trailing spaces + high cardinality = key column)
```

### Step 3: RUN

Now — and only now — run the tool.

Capture the full output. Do not summarize. Do not paraphrase. Capture it.

### Step 4: COMPARE

Go through your predictions one by one:

| # | Prediction | Actual | Match? | Classification |
|---|-----------|--------|--------|---------------|
| 1 | null_pct → 0.0 | null_pct = 0.0 | ✅ | — |
| 2 | distinct_normalized → ~970 | distinct_normalized = 985 | ❌ | BUG or MISUNDERSTANDING? |
| 3 | trailing_spaces finding | not present | ❌ | Investigate |

**Every prediction must be checked.** No exceptions.

### Step 5: CLASSIFY

Every discrepancy (❌) gets exactly one classification:

| Classification | Meaning | Action |
|---|---|---|
| **BUG** | The source code should produce this output but doesn't | File as bug, investigate root cause |
| **MISUNDERSTANDING** | You misread the source or the data | Correct your understanding, document |
| **DOCUMENTATION GAP** | The source is correct but the behavior is non-obvious | Add docstring/comment/test |
| **THRESHOLD ISSUE** | Logic is correct but threshold is wrong for this data | Evaluate whether threshold needs tuning |
| **DATA ISSUE** | Your test data doesn't exercise the code path | Create better test data |

**"It's probably fine" is not a classification.** Every discrepancy gets a root cause.

### Step 6: RETAIN EVIDENCE

Retain the prediction/comparison/classification as bounded session evidence. Do not turn
routine output into learning content.

### Step 7: GATE AND ASSESS

After the applicable gate succeeds, assess genuine reusable learning. Record bounded
Observation IDs, or close with `nothing_reusable_learned`.
Content quality and the firewall remain global lifecycle invariants.

## Anti-Rationalization: Dogfooding Edition

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "The output looks right, I don't need to read the source" | You can't know what "right" looks like without reading the source. You're pattern-matching on plausibility, not correctness. | Read the source first. Always. |
| "I'll read the source if something looks wrong" | If you don't know what "right" looks like, you can't recognize "wrong." Bugs that produce plausible output are the dangerous ones. | Read first. Predict first. Then run. |
| "I already know how this function works" | You know what you THINK it does. The source code knows what it ACTUALLY does. Those are different. | Read it again. Every time. |
| "The tests pass, so the tool works" | Tests verify test cases. Dogfooding verifies real-world behavior on real data. Tests can pass while the tool produces wrong output on your actual data. | Tests ≠ dogfooding. Both are required. |
| "I'll predict broadly — it should detect some issues" | Broad predictions are unfalsifiable. "Some issues" matches everything. You've predicted nothing. | Specific, falsifiable predictions only. |
| "I'll compare the important parts" | Every part is important. The metric you skip is the one that's wrong. | Compare everything. Line by line. |
| "This discrepancy is minor" | Minor discrepancies compound. And "minor" is a judgment you're making without investigating. | Classify it. Then decide if it's minor. Not before. |
| "I'll investigate that discrepancy later" | Later means never. The context you have now will be gone. | Investigate now. Classify now. Log now. |
| "The function is too complex to predict" | Then you don't understand it well enough to validate it. That's exactly why you need to read more. | Read until you can predict. No shortcut. |
| "I just need to verify it runs without errors" | "Doesn't crash" is the lowest bar. A function can run perfectly and return completely wrong results. | Verify correctness, not just execution. |

## Dogfooding Depth Levels

Not every dogfood pass needs to be exhaustive. Match depth to risk:

### Quick Pass (new feature, first validation)
- Read the main function and relevant helpers
- Use the smallest set of predictions and inputs that exercises the changed behavior
- Full comparison

### Standard Pass (pre-release, important tool)
- Read the full module
- Cover material normal, boundary, and failure behavior
- Include edge cases (nulls, empty, single-value, all-null)
- Full comparison + classification

### Deep Pass (critical tool, post-bug-fix, regression-prone)
- Read all related modules
- Use enough predictions and inputs to cover critical and adversarial paths
- Include adversarial inputs
- Compare against prior version output
- Full comparison + classification + root cause for every discrepancy

## Common Dogfooding Targets

### Tool Output Dogfooding
**Target:** A function's return value
**Read:** The function source + its helpers
**Predict:** Specific metrics, findings, risks for given input
**Compare:** Predicted vs actual output

### Enforcement Dogfooding
**Target:** RuntimeError gates and enforcement logic
**Read:** The enforcement code (bootstrap.py + _dispatcher/* dispatcher, gate checks)
**Predict:** Which action sequences should be blocked vs allowed
**Compare:** Does the system actually block/allow correctly?

### Integration Dogfooding
**Target:** Wrapper fidelity (e.g., Anchor wrapper around a standalone tool)
**Read:** Both the wrapper and the underlying tool
**Predict:** Output shape, field mapping, sample limits
**Compare:** Does the wrapper faithfully translate the underlying output?

### Regression Dogfooding
**Target:** Output stability across code changes
**Read:** The diff of what changed
**Predict:** Which outputs should change and which should stay stable
**Compare:** Use `anchor("dogfood")` to compare before/after

## The Meta-Rule

```
If you can't predict the output, you don't understand the tool.
If you don't understand the tool, you can't validate it.
If you can't validate it, you're not dogfooding — you're demoing.

Demos prove tools run.
Dogfooding proves tools work.

NEVER validate a tool without reading its source code first — source-first dogfooding.
NEVER ship a tool change without running it on real data — `anchor("dogfood", current_output)` is the verification step.
```
