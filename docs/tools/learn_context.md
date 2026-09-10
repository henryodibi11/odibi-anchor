# learn_context

`learn_context` is a compatibility-only historical recovery API used when old persisted debt
technically requires its event-payload behavior. Normal learning uses
`anchor("learning", "capture|assess", ...)`. The callable processes caller-supplied events and
may append candidate memory entries. Runtime confirmation/promotion is unavailable; legacy
confirmation events return a blocked finding and do not change candidate authority.

```text
What should I remember from this session?
Did I learn anything that helps future sessions?
```

## Public API

```python
from odibi_anchor.codebase import learn_context

def learn_context(
    root: str | Path,
    *,
    session_events: list[dict[str, Any]] | None = None,
    tool_usage: list[dict[str, Any]] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `session_events` | list[dict] or None | None | Caller-supplied compatibility events; confirmation requests remain blocked |
| `tool_usage` | list[dict] or None | None | Tool usage telemetry for pattern detection |
| `subject` | str or None | None | Human label |
| `output_format` | str | "dict" | "dict" or "markdown" |

### Event Format

```python
{"type": "error", "text": "TypeError...", "fix": "added missing param", "resolved": True}
{"type": "confirm", "memory_id": "m0003"}  # compatibility-only; promotion unavailable
{"type": "discovery", "detail": "Source timestamps arrive in local time"}
```

On the compatibility-only historical route, used only when old persisted debt technically
requires it, `learn_context` persists events supplied by the caller; it does not collect or inject
events from session history. An omitted or empty list is a no-op. Normal file-changing tasks close
learning through canonical capture/assess rather than this compatibility API.

Through the MCP gateway, pass events as a keyword inside the serialized JSON object:

```python
anchor_execute(
    "learn",
    '{"session_events":[{"type":"discovery","detail":"what was learned"}]}',
)
```

## Output Shape

```python
{
    "kind": "learn_context",
    "metrics": {"events_processed": 3, "memories_added": 1, "memories_confirmed": 0},
    "memories_added": [{"id": "m0010", "type": "failure_pattern", ...}],
    "memories_confirmed": [],
    "suggested_next_actions": ["SHOULD: Review new memories for accuracy..."],
}
```

## When To Use

- Only for compatibility recovery of historical event payloads that technically require it.
- Normal learning uses `anchor("learning", "capture|assess", ...)`; normal task memory use is
  recorded through disposition, application, and evidence-backed evaluation.
