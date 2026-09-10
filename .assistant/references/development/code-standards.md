# Anchor House Engineering Standard

> Profile: `anchor-house-engineering/v1`. Advisory development guidance; it is not a
> discoverable skill, executable policy, approval, or effect grant.

Use this hub for source changes selected by the reference resolver. Apply the
[Python profile](code-standards-python.md) and
[data/platform profile](code-standards-data-platform.md) only when their stated
signals match. Repository-native rules and tools remain the source of local detail.

## Authority and precedence

Resolve conflicts in this order; the stricter applicable requirement wins:

1. Platform and system safety, effect, evidence, artifact, lifecycle, and compliance controls.
2. Repository instructions and executable configuration, including supported runtimes and tools.
3. Approved project and Spec decisions, followed by documented local conventions.
4. This universal house standard.
5. Applicable language and data/platform profiles.
6. Examples and advisory heuristics.

A lower layer may fill a gap but may not weaken a higher layer. Surface a contradiction
with both sources instead of silently blending them. Reference resolution grants no
skill, effect, approval, filesystem authority, or evidence status.

## Universal principles

- **Understand before editing.** Identify the existing contract, owners, callers,
  compatibility surface, and smallest coherent change.
- **Make behavior changes intentional.** Preserve observable behavior and compatibility
  unless the requested change and its evidence say otherwise.
- **Optimize for the next reader.** Prefer clear names, explicit boundaries, cohesive
  ownership, and reviewable diffs over compact or surprising constructions.
- **Expose consequential behavior.** Make inputs, outputs, dependencies, side effects,
  failure behavior, and state transitions visible at the boundary that owns them.
- **Protect trust boundaries.** Validate untrusted or cross-system inputs where they
  enter; do not repeat validation throughout trusted internals without a reason.
- **Keep one source of truth.** Reuse the owning contract or helper rather than copying
  policy, state, or derived logic into another layer.
- **Follow the repository first.** Use its supported runtime, conventions, formatter,
  linter, type checker, test runner, build process, and established abstractions.
- **Test observable outcomes.** Cover important behavior, compatibility, and failure
  modes in proportion to risk; avoid assertions that merely mirror implementation.
- **Leave a consistent result.** Keep code, tests, documentation, and operational
  evidence aligned, and report each executed check as passed, failed, or unavailable.

## Review questions

- Is this the smallest complete change under the owning contract?
- Are failure paths and side effects visible and proportionately tested?
- Did the change create a second source of truth or bypass a repository-native tool?
- Do the diff and retained results support every delivery claim?

## Golden examples

These pairs illustrate judgment. They are neither syntax templates nor gate conditions.

### Example 1 — Principle: clarify only when named steps help

```python
# Dense: several decisions are hidden in one expression.
eligible = {k: [x for x in items if x.active] for k, items in groups.items() if items}

# Clearer here: the intermediate has domain meaning.
eligible = {}
for group, items in groups.items():
    active_items = [item for item in items if item.active]
    if active_items:
        eligible[group] = active_items
```

### Example 2 — Principle: preserve failure context at a boundary

```python
# Weak: the original failure disappears.
except OSError:
    raise ConfigError("Configuration could not be loaded")

# Better: add actionable boundary context and retain the cause.
except OSError as error:
    raise ConfigError(f"Could not load configuration from {path}") from error
```

### Example 3 — Principle: evolve a public contract compatibly

```python
# Before
def render(report, *, compact=False): ...

# After: existing callers keep their behavior.
def render(report, *, compact=False, include_notes=True): ...

def test_render_can_omit_notes():
    result = render(sample_report, include_notes=False)
    assert "Notes" not in result
```

### Example 4 — Principle: separate transformation from persistence

```python
# Coupled: transformation and write cannot be reasoned about independently.
def normalize_and_save(records, store):
    store.write(normalize(records))

# Separated: the transform is observable before the authorized effect.
def normalize(records):
    return [normalize_record(record) for record in records]

store.write(normalize(records))
```
