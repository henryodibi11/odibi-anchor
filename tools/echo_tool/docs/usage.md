# echo — Usage Guide

## Entry Point

```python
echo_context(
    message: str = "",
    repeat: int = 1,
    output_format: str = "dict",
) -> dict | str
```

Via dispatcher: `anchor("echo", message="hello")`

---

## Parameters

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `message` | `str` | `""` | Yes | The message to echo back. Also accepted as first positional argument. |
| `repeat` | `int` | `1` | No | Number of times to repeat the message in output. Joined with newlines. |
| `output_format` | `str` | `"dict"` | No | `"dict"` returns Anchor contract dict. `"markdown"` returns rendered string. |

---

## Examples

```python
# Basic echo
ctx = anchor("echo", message="Hello, world!")
# -> {"kind": "echo", "summary": "Echo: Hello, world!", ...}

# Positional argument
ctx = anchor("echo", "Testing 1-2-3")
# -> {"kind": "echo", "summary": "Echo: Testing 1-2-3", ...}

# Repeated message
ctx = anchor("echo", message="ping", repeat=3)
# -> findings: ["ping\nping\nping"]

# Markdown output
print(anchor("echo", message="test", output_format="markdown"))
# Renders as formatted markdown with metrics table
```

---

## Output Contract

Returns a standard Anchor contract dict:

| Key | Type | Value |
|---|---|---|
| `kind` | `str` | `"echo"` |
| `subject` | `str` | `"echo"` |
| `summary` | `str` | `"Echo: {first 50 chars of message}"` (truncated with `...` if longer) |
| `metrics.message_length` | `int` | Length of original message |
| `metrics.repeat` | `int` | Repeat count used |
| `metrics.output_length` | `int` | Length of final repeated string |
| `findings` | `list[str]` | `[repeated_message]` — the echoed output |
| `risks` | `list` | Always `[]` |
| `samples.raw_message` | `str` | Original unmodified message |
| `suggested_next_actions` | `list[str]` | Hints about tool registry usage |

---

## Error Cases

| Error | Cause | Fix |
|---|---|---|
| `ValueError: echo tool requires 'message' argument` | Empty or missing message | Provide a non-empty `message` string |

---

## Use Cases

- **Verify tool registry wiring**: After registering a new tool, call `anchor("echo", message="test")` to confirm the dispatcher routes correctly.
- **Test output_format rendering**: Compare `output_format="dict"` vs `output_format="markdown"` to verify rendering pipeline.
- **Reference implementation**: Study this tool’s code when building new Anchor tools — it shows the minimum contract shape without any complex logic.
