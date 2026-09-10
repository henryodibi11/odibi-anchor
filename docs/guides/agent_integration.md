# Agent Integration Guide

## How AI Agents Use odibi_anchor

### Pattern: Plan-Select-Verify

```python
from odibi_anchor.planning import task_execution_context

def agent_workflow(task, **kwargs):
    ctx = task_execution_context(task, **kwargs)
    
    # Gate 1: Is the task clear enough?
    if ctx["status"] == "under_specified":
        return {"action": "ask_user", "questions": ctx["hints"]["clarifying_questions"]}
    
    # Select evidence questions before execution. Candidate actions are possible
    # channels, not proof that runtime support or sufficient evidence exists.
    required = [
        question for question in ctx["context_plan"]["questions"]
        if question["priority"] == "required"
    ]
    return {
        "action": "evaluate_context",
        "required_questions": required,
        "plan": ctx["plan"],
        "criteria": ctx["verification"]["acceptance_criteria"],
    }
```

### Pattern: Agent Handoff

```python
# Agent A generates context for Agent B
ctx = task_execution_context(
    task="Test the new artifact in Databricks.",
    mode="testing",
    audience="agent",
    requester="ChatGPT",
    executor="Genie Code",
    # ... full context
)

# The prompt_brief is ready to paste into another agent
handoff_prompt = ctx["handoff"]["prompt_brief"]
```

### Pattern: Pre-Write Validation

```python
from odibi_anchor.validation import quality_gate_context

def safe_write(df, target, keys):
    """Only write if quality gate passes."""
    result = quality_gate_context(df, keys=keys)
    
    if not result["metrics"]["is_write_safe"]:
        raise ValueError(
            f"Quality gate failed: {result['summary']}\n"
            f"Fix: {result['fix_all_expr']}"
        )
    
    # Safe to proceed
    saver.save(df, target)
    return result
```

### Pattern: Bounded Context Selection

`task_execution_context` selects a bounded set of provider-neutral evidence questions.
Each question may include candidate `anchor()` actions:

```python
ctx = task_execution_context(task="...", mode="implementation")

for question in ctx["context_plan"]["questions"]:
    print(question["priority"], question["question"])
    for candidate in question["candidate_actions"]:
        print(candidate["name"], candidate["availability"])
```

`availability="registered"` means only that the dispatcher recognizes the action.
`availability="unknown"` means direct planning did not receive dispatcher metadata.
Neither status proves dependencies, credentials, permissions, current evidence, or
question completeness. `called_this_session` records invocation only. The legacy
`discovery.recommended_context_generators` field is a compatibility projection of
these same candidates, not a second catalog.

### Pattern: Self-Monitoring

```python
# Track task intake quality over time
metrics = ctx["metrics"]
# → constraint_count, risk_count, evidence_count, artifact_count, etc.

# Check whether the task statement is ready to send to an agent
if ctx["hints"]["ready_to_ask_ai"]:
    # This does not establish runtime safety or evidence completeness.
    pass
```

## Output Formats

All generators support two output formats:
- **dict** (default): Machine-readable, JSON-serializable, for programmatic use
- **markdown**: Human-readable, for display or logging

```python
# Dict (for agents)
result = quality_gate_context(df, keys=["id"], output_format="dict")

# Markdown (for humans)
report = quality_gate_context(df, keys=["id"], output_format="markdown")
print(report)
```
