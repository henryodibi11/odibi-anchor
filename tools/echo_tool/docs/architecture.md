# echo — Architecture

## Overview

The `echo` tool is a proof-of-concept implementation for the Anchor Tool Registry. It echoes back a message as a standard Anchor contract, demonstrating the minimum requirements for a registered tool.

---

## Pipeline

```
Input (message, repeat)
    │
    ├─ Resolve message from positional arg or keyword
    │
    ▼
Validate message is non-empty
    │
    ▼
Repeat message N times (join with newlines)
    │
    ▼
Build Anchor contract:
    ├─ kind: "echo"
    ├─ metrics: message_length, repeat, output_length
    ├─ findings: [repeated_message]
    └─ samples: {raw_message: original}
    │
    ▼
Return dict or render as markdown
```

---

## Design Decisions

1. **Minimal implementation**: The entire tool is 57 lines with zero external dependencies beyond Python’s standard library. This makes it the ideal reference implementation for new tool authors.

2. **tool.json registry format**: Demonstrates all required fields:
   - `name`, `version`, `description`
   - `category`: `"diagnostics"`
   - `entry_point`: `"echo_impl:echo_context"` (module:function)
   - `renderer`: `null` (uses inline markdown rendering)
   - `inputs.required`: `["message"]`
   - `inputs.optional`: `["repeat"]`
   - `outputs.contract`: `"StandardContract"`

3. **Anchor output contract compliance**: Returns the standard contract shape (kind, subject, summary, metrics, findings, risks, samples, suggested_next_actions) proving the registry can dispatch to it like any other tool.

4. **Dual output format**: Supports both `output_format="dict"` and `output_format="markdown"` with an inline renderer (no `finalize_context` — renders directly in the function body).

---

## Dependencies

None. Zero imports from `odibi_anchor` internals. This is intentional — the tool proves that the registry can dispatch to a completely standalone module.

---

## Use Cases

- Verify tool registry wiring after adding a new tool
- Test that `output_format` rendering works end-to-end
- Reference implementation when creating new tools
- Smoke test the dispatcher’s routing logic
