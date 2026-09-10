# Writing Specs

> Preserved technique source for the native owner.

Produce specs thorough enough that any agent can implement them mechanically.

## Core Principle

**The spec is the product. Code is the artifact.** A good spec removes all judgment from implementation — the agent just transcribes.

## Required Sections

Every spec MUST include these sections in order:

### 1. Overview
- What the feature/change does (2-3 paragraphs max)
- Why it's needed — the specific problem it solves
- What it does NOT do (scope boundary)

### 2. Current State — Evidence from Source
- Read the actual source files before writing anything
- Include real line numbers, real function signatures, real return shapes
- Quote actual code — never assume or guess
- Show what exists today that this spec builds on or changes

### 3. What We're Adding
- Numbered list of concrete additions
- Each addition includes:
  - **Why** (one sentence)
  - **Implementation** (actual Python with docstring, not pseudocode)
  - **Where it's exposed** (return dict key, new param, etc.)

### 4. Signature Changes
- Show before/after signatures with exact params
- Mark NEW params explicitly
- Call out any breaking changes and why they're safe

### 5. Return Dict Changes
- Show the exact dict shape with NEW keys marked
- Include types for every new field

### 6. Implementation Steps
- Ordered table: Step | File | Description
- Each step is independently committable
- Include which tests to add at each step

### 7. Success Criteria
- Concrete, testable assertions — not prose
- Written as near-executable test descriptions
- Example: `anchor("map")["reverse_dependency_graph"]` is populated for any project with internal imports
- Name the focused and broader verification warranted by the change's risk and shared contracts

### 8. Design Decisions
- Table of Decision | Rationale
- Include only material decisions and assumptions; do not create entries to meet a quota
- What you're NOT doing and why — this prevents scope creep during implementation
- Deferred items with clear reasons
- Include residual uncertainty and, where relevant, rollout, rollback, stop, and reversal conditions

### 9. Complexity Estimate
- Low / Medium / High
- Estimated new lines across which files
- Number of sessions to complete

## Rules

1. **Read before writing.** Never spec against assumptions. Open the source, find the line numbers, quote the actual signatures.
2. **Include actual code.** Every new function in the spec must have a real implementation with docstring, not a description of what it should do.
3. **Spec the tests.** Success criteria must be specific enough to write `assert` statements from.
4. **Scope aggressively.** Every spec needs a "What We Are NOT Doing" section. If it's not in scope, say so explicitly.
5. **One spec per feature.** Don't combine unrelated changes. Each spec is a self-contained work order.
6. **Backward compatible by default.** New params have defaults. Existing callers don't break. Call out any exceptions.
7. **File the spec, don't implement.** The goal of this skill is to produce a spec in `specs/`, not to write code. Implementation is a separate task.

## Output

Save the spec through `anchor("spec", ...)`; it routes to the active managed project's
artifact root as `specs/{FEATURE_NAME}_SPEC.md`. Do not place durable specs in an
external target repository. When a Problem Record produced the recommendation, use
`anchor("spec", "from_problem", "PRB-...", name="FEATURE_NAME")` so traceability is retained.

Every spec file MUST include a YAML frontmatter block at the top:

```yaml
---
status: draft                       # draft | ready | in-progress | done
complexity: medium                  # low | medium | high
estimated_sessions: 2
phases: []                          # List of {name, status, estimated_sessions}
success_criteria: []                # Verifiable criteria
files_touched: []                   # Files this spec will modify
---
```

Status meanings:
- **draft** — idea captured, not yet reviewed or implementation-ready
- **ready** — reviewed, implementation-ready, waiting for a session
- **in-progress** — actively being implemented
- **done** — implemented and verified

## Spec Lifecycle (anchor() actions)

After writing a spec manually or via `anchor("task", mode="spec_creation")`:

```
anchor("spec", "persist", task_result)   # Save task output as spec file
anchor("spec", "review", "NAME")         # Audit against standards before execution
anchor("spec", "validate", "NAME")       # Check staleness (files moved, imports broken?)
anchor("spec", "execute", "NAME")        # Begin mechanical execution from spec
anchor("spec", "done", "NAME")           # Mark complete
```

Other useful commands: `anchor("spec")` lists all specs, `anchor("spec", "status", "NAME")` shows details.

### Enforcement

Use this skill when the accepted task explicitly requests a formal Spec or the
task policy marks one required because the change is consequential. A mode name
alone does not create that requirement.

When required, persist the Spec and run `anchor("spec", "review")`; consequential
effects remain blocked until the exact linked Spec has a rating ≥ **"good"**.
Review includes:

- **Check 8: standards_cross_ref** — verifies the Spec references skills required by its accepted task
- Missing skill references → review fails → must add references and re-review

**Sequence when a formal Spec is required:**
```
anchor("task", mode="implementation")     # Returns the task-policy decision
anchor("skill_loaded", "writing-specs")   # Register this skill
anchor("spec", "persist", task_result)    # Persist spec (blocked without this)
anchor("spec", "review", "NAME")         # Must get rating ≥ "good"
# Consequential effects are now unblocked
```

**To pass Check 8**, the Spec must reference the direct skills required by the
accepted task and the relevant requirements from each one.

## Reference

See existing specs in `specs/` for the proven format:
- `AGENT_INIT_REVAMP_SPEC.md` — structural refactor example
- `MAP_DEEPEN_SPEC.md` — feature deepening example (cleanest template)
- `VALIDATE_DEEPEN_SPEC.md` — new capabilities + manifest integration example
- `CONTEXT_FRAME_SPEC.md` — new module with phased delivery example

---

## Compliance Protocol

> **Root cause of the pattern:** A spec that defines only deliverables (READMEs, files, features) but not protocol checkpoints will cause the agent to skip `anchor("review")` and `anchor("gate")` when implementation momentum is high. The spec must be the enforcement mechanism — not just the roadmap.

### Required: Compliance Requirements Section

Every spec MUST include a **Section 10: Compliance Requirements** block. Without it, the agent has no gate anchor and will deliver without running the protocol.

```markdown
## 10. Compliance Requirements

### Source-Change Cycle
Each coherent implementation boundary maps to the runtime-required source-change cycle;
one cycle may contain multiple related deliverables:
  task → [edit files] → anchor("review") → anchor("gate") → truthful learning assessment

No source-change delivery boundary is complete until anchor("gate") returns with obligations_paid.

### Delivery Boundary Closure
Mark which phases share one coherent source-change boundary and which are independently
gated. A planning or read-only phase has no manufactured gate or assessment.

| Source-change boundary | Related phases/deliverables | Gate must verify |
|---|---|---|
| Boundary 1 | [list related phases/files/tools] | touched + applicable preflight/tests |
| Boundary 2 | [list related phases/files/tools] | touched + applicable preflight/tests |
| ...     | ...               | ...                                       |

### Acceptance Criteria (compliance-aware)
- [ ] anchor("gate") passes for every actual source-change delivery boundary in this spec
- [ ] Each gated boundary assesses real Observation IDs or records `nothing_reusable_learned`
- [ ] No file was modified without a corresponding anchor("touched") call
- [ ] Relevant focused and broader risk-based verification passes for each gated boundary
```

### Why Output-Only Specs Fail

| Spec says | Agent does | Result |
|---|---|---|
| "Write 3 READMEs and validate examples" | Writes 3 READMEs, validates examples, done | `anchor("review")` and `anchor("gate")` skipped — no compliance trail |
| "Delivery complete when all tools have READMEs" | Checks off tools, delivers | Applicable source-change gate never triggered |
| "Success = examples run without errors" | Runs examples, passes, delivers | Gate and truthful learning assessment omitted |

Contrast with a compliance-embedded spec:

| Spec says | Agent does | Result |
|---|---|---|
| "Source-change delivery complete when anchor('gate') passes" | Can't deliver changed source without running gate | Compliance enforced at the real boundary |
| "Related READMEs share one reviewed docs boundary" | Reviews and verifies the complete coherent change | No arbitrary per-file ceremony |
| "Acceptance criteria include anchor('gate') passing" | Can't claim done without gate evidence | Delivers with audit trail |

### Template Language (copy into your spec)

**For a phased delivery spec:**
```markdown
## 10. Compliance Requirements
Declare the phases that form each coherent source-change delivery boundary.
Before crossing such a boundary, review and verify the complete change, pass anchor("gate"),
and close the resulting learning assessment truthfully.
```

**For a single-feature spec:**
```markdown
## 10. Compliance Requirements
This spec maps to one feature cycle: task → edit → review → gate → truthful assessment.
Acceptance: anchor("gate") passes with obligations_paid covering all modified files.
```

**For a multi-README or multi-tool docs spec:**
```markdown
## 10. Compliance Requirements
Treat related README edits as one coherent docs delivery unless the Spec defines an
independent approval or runtime boundary. Validate examples, review the complete change,
then gate and assess once at that boundary.
```

---

## Anti-Patterns

| Anti-pattern | Why it fails | Fix |
|---|---|---|
| Spec defines delivery only by output count | Agent completes output and skips the applicable source-change gate | Name the coherent source-change boundary and its gate evidence |
| Success criteria are purely functional ("examples run without errors") | No compliance verification in the criteria | Add: "anchor('gate') passes" as an explicit criterion |
| Spec has no Section 10 | Agent has no structural reason to run gate | Always include Compliance Requirements section |
| Spec describes source changes but no delivery boundary | Agent delivers edits without closure evidence | State the coherent boundary and required review, verification, gate, and assessment |
| "The spec is clear so the agent will know to gate" | Agents optimize for task completion, not compliance, under momentum | Compliance must be stated explicitly — clarity about output ≠ clarity about protocol |

Each actual source-change delivery boundary ends with a gate and truthful assessment. A
phase that does not cross such a boundary creates no synthetic gate or memory obligation.
