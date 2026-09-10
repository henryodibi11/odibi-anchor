# Capture examples

## Observation versus cause

**Bad:** “Databricks caused the write race.”

**Good:** “On Databricks serverless, `os.link(temp, target)` returned `EPERM` in run R1.
The same path succeeded through `os.rename` after the error. This establishes an unsupported
hard-link operation for the observed environment; it does not establish all Databricks
filesystems behave this way. Evidence: retained traceback and successful rerun.”

## Evidence status

**Bad:** “All checks passed (Spark tests unavailable).”

**Good:** “Pure-Python tests: passed, 37/37. Spark-dependent tests: skipped, 52, because no
Spark session was available. Full Spark behavior remains unverified.”

## Decision

**Bad:** “Use Pydantic because it is good.”

**Good:** “Decision: use Pydantic only at the public configuration boundary. Evidence:
three input shapes require coercion and structured errors. Alternative: dataclasses plus
manual validation. Reversal: remove Pydantic if bundle constraints prohibit it. Owner: HCO.”

## Memory candidate

**Bad:** “Always use `repository_scope=['.']`. Confidence 1.0.”

**Good:** “For the observed Databricks Git Folder project, the accepted package path was
`declarative_visualization_engine`; using `declarative_viz` failed scope validation.
Applicable only to that repository layout. Evidence: task rejection plus repository tree.
Candidate should be superseded if the package is renamed.”

## Terminal return

**Bad:** “Done. Everything is green and pushed.”

**Good:** “Status: completed. Repository: owner/repo; branch: feature/x; revision: abc123.
Changed: `src/x.py`, `tests/test_x.py`. Executed: `pytest tests/test_x.py` — 8 passed.
Full suite unavailable due to time limit. Anchor review passed; gate passed; learning assessed.
No push, merge, deployment, or publication performed. Residual: platform integration is
untested. Next authority: owner approval to push.”

## Routing one finding

A failed API call is journaled automatically. If noteworthy, capture an Observation with
the exact response and uncertainty. If reproducible and material, bind it as Evidence to a
Problem. If a response is chosen, record a Decision. If behavior must be precise, write a
Spec. Only then authorize implementation through a Work Item. Repeated operational insight
may be assessed into memory; none of these transitions is automatic.
