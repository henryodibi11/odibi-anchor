# dogfood_regression_context

**Package:** `odibi_anchor.profiling`  
**Purpose:** Store dog-food output baselines, diff on re-run, flag regressions vs improvements.

## When to Use

- After modifying a tool's behavior — verify output quality didn't regress
- During development — track whether changes improve or degrade results
- Before merging — confirm no accidental quality loss

## Usage

```python
from odibi_anchor.profiling import dogfood_regression_context, exploration_context

# First time: save baseline
current = exploration_context(root, df)
ctx = dogfood_regression_context(current, save_as_baseline=True, root="/path/to/project")

# Later: compare against baseline
ctx = dogfood_regression_context(current, root="/path/to/project")
if ctx["metrics"]["regressions_count"] > 0:
    print("REGRESSION detected!")
    for d in ctx["diffs"]:
        if d["verdict"] == "regression":
            print(f"  {d['field']}: {d['baseline']} → {d['current']}")
```

## Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `current_output` | `dict` | required | Tool output dict (must have `kind` key) |
| `baseline_dir` | `str` | `".dogfood_baselines"` | Directory for baseline files |
| `subject` | `str \| None` | from output | Override subject label |
| `save_as_baseline` | `bool` | `False` | Save current as new baseline |
| `root` | `str \| None` | cwd | Project root for resolving paths |
| `output_format` | `str` | `"dict"` | `"dict"` or `"markdown"` |

## Output Contract

```python
{
    "kind": "dogfood_regression_context",
    "subject": "exploration_context::silver.queue_positions",
    "summary": "2 improvement(s), 0 regression(s), 0 unchanged vs baseline",
    "metrics": {
        "improvements_count": 2,
        "regressions_count": 0,
        "unchanged_count": 0,
        "baseline_age_hours": 48.3,
    },
    "diffs": [
        {
            "field": "metrics.coverage_gaps_count",
            "baseline": 7,
            "current": 3,
            "verdict": "improvement",
            "reason": "value improved from 7 to 3",
        },
    ],
    "baseline_path": ".dogfood_baselines/exploration_context__silver_queue_positions.json",
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

## Diff Classification

| Metric direction | Improvement | Regression |
| --- | --- | --- |
| `completeness_pct` (higher=better) | value increased | value decreased |
| `null_pct` (lower=better) | value decreased | value increased |
| `violation_count` (lower=better) | value decreased | value increased |
| `risks` (list) | fewer items | more items |

## Baseline Storage

- Stored as JSON in `{root}/{baseline_dir}/{kind}__{subject_slug}.json`
- Each baseline includes `saved_at`, `saved_at_epoch`, and `output` fields
- **Gitignored** by default (environment-specific row counts, timestamps)

## Render Function

```python
from odibi_anchor.profiling import render_dogfood_regression_report

md = render_dogfood_regression_report(ctx)
```
