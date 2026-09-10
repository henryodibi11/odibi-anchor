# Test standards reference

> Preserved technique source for the native owner.

Use this workflow when test design, test implementation, fixture repair, or a coverage
gap is an explicit outcome. Routine execution of existing checks does not by itself
activate the skill. Apply repository test policy first and only the house profiles whose
applicability signals match the code under test.

## Translate risk into behavior

For each material requirement or failure risk, state:

- observable behavior and the boundary where it can be seen;
- representative input or precondition;
- expected output, state, effect, or failure;
- why the case could detect a real regression.

Cover the normal contract, consequential boundary conditions, compatibility behavior,
and relevant failure modes. Do not turn every branch or implementation step into a test.

## Choose the lowest useful level

| Level | Use when |
| --- | --- |
| Unit | A deterministic contract can be isolated without hiding the behavior under test. |
| Component | Several collaborators define the outcome and a fake would erase their contract. |
| Integration | A database, runtime, protocol, filesystem, or framework boundary is the risk. |
| End to end | The composed user or operator path is itself the contract. |

Use platform-specific fixtures and markers only when the matching platform is detected
and its behavior is under test. A fast substitute cannot prove engine-specific semantics;
a heavy runtime adds no value when a pure contract is sufficient.

## Fixtures and doubles

- Build the smallest deterministic fixture that preserves the behavior's important
  grain, ordering, null, identity, or state characteristics.
- Make time, randomness, environment, and external responses explicit when they affect
  the assertion.
- Prefer public fakes or boundary doubles over mocks of private call sequences.
- Keep shared fixtures cohesive; localize data that only one scenario understands.
- Never depend on ambient accounts, mutable shared data, or execution order unless that
  dependency is the contract being tested and is safely isolated.

## Assertions

Assert outcomes, invariants, and material side effects. Include enough diagnostic context
to identify the failed contract without reproducing the implementation in the assertion.
For collections or tabular results, declare whether order, schema, duplicates, nulls,
precision, and extra fields matter. For errors, assert the public error type and meaningful
context rather than a complete incidental message.

## Regression proof

A regression test should fail for the targeted defect or missing behavior and pass for
the intended correction. Demonstrate that relationship through an observed failing run,
a controlled mutation, or another bounded method when practical. If the pre-fix failure
cannot be reproduced, report that limitation instead of overstating the proof.

## Verification and delivery

Run the narrowest decisive test first, then the repository-required broader checks for
the affected contract. Test invocation, test count, and coverage percentage are not
behavioral proof by themselves. Record command or method, scope, pass/fail/skip status,
and decisive output. Unavailable, flaky, skipped, or blocked checks remain non-passing.

Follow [risk-based planning](planning.md) for a test campaign and
[real-client scenarios](scenarios.md) when client or transport realism is material.
The global lifecycle owns touched-path registration, preflight, review, gate, and learning;
this reference does not manufacture those results or weaken their requirements.
