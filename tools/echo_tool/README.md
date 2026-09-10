# Echo Tool

> Proof-of-concept tool for the Anchor Tool Registry — echoes back a message as a standard Anchor contract.

---

## When to Use

- Verify tool registry wiring after registering a new tool
- Test `output_format` rendering end-to-end
- Reference implementation for new tool authors
- Smoke test the dispatcher’s routing logic

---

## Quick Start

```python
# Via dispatcher
ctx = anchor("echo", message="Hello, world!")
print(ctx["summary"])    # "Echo: Hello, world!"
print(ctx["findings"])   # ["Hello, world!"]

# With repeat
ctx = anchor("echo", message="ping", repeat=3)
print(ctx["findings"])   # ["ping\nping\nping"]
```

---

## Parameters

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `message` | `str` | `""` | Yes | Message to echo |
| `repeat` | `int` | `1` | No | Times to repeat |
| `output_format` | `str` | `"dict"` | No | `"dict"` or `"markdown"` |

---

## Output

Returns a standard Anchor contract:

```python
{
    "kind": "echo",
    "subject": "echo",
    "summary": "Echo: Hello, world!",
    "metrics": {"message_length": 13, "repeat": 1, "output_length": 13},
    "findings": ["Hello, world!"],
    "risks": [],
    "samples": {"raw_message": "Hello, world!"},
    "suggested_next_actions": [...]
}
```

---

## Documentation

Detailed documentation is available in the `docs/` directory:

- [Architecture](docs/architecture.md) — Pipeline, design decisions, tool.json format
- [Usage Guide](docs/usage.md) — Full parameter reference, output contract, examples
