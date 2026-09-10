# failure_pattern_context

`failure_pattern_context` matches error text against a catalog of known failure
patterns (stored in YAML or passed directly). It answers:

```text
Have I seen this error before?
What's the known root cause and fix?
What approaches should I avoid?
```

## Public API

```python
from odibi_anchor.debugging import failure_pattern_context

def failure_pattern_context(
    error_text: str,
    *,
    patterns_path: str | Path | None = None,
    patterns: list[dict[str, Any]] | None = None,
    subject: str | None = None,
    max_matches: int = 3,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `error_text` | str | required | The error message/traceback to match |
| `patterns_path` | str or Path or None | None | Path to failure_patterns.yaml |
| `patterns` | list[dict] or None | None | Patterns list (alternative to file) |
| `subject` | str or None | None | Human label |
| `max_matches` | int | 3 | Maximum patterns to return |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "failure_pattern_context",
    "metrics": {"total_patterns": 12, "has_match": True, "match_count": 1},
    "matched_patterns": [
        {"pattern": "...", "root_cause": "...", "fix": "...", "never_try": [...]},
    ],
    "samples": {"never_try": ["approach1", "approach2"]},
    "suggested_next_actions": ["MUST: Apply fix — ...", "MUST: Avoid these approaches: ..."],
}
```

## When To Use

- As the first step when encountering an error (before debugging from scratch)
- To check if an error has a known fix before spending time investigating
- To find approaches that are known to NOT work for this error
