# Assurance outcome qualification

Outcome qualification measures whether an exact assurance control version improves observable
work outcomes. It does not treat invocation, artifact production, schema validity, or an aggregate
score as proof. The public implementation lives only in `odibi_anchor.assurance`.

## Current status

The packaged corpus and synthetic tests qualify the **infrastructure contract only**. They do not
constitute real dogfood, elapsed promotion windows, independent agent-family or host coverage,
sealed-holdout adjudication, or independent approval. Until those observations exist, promotion
evidence is pending or unavailable and `assess_promotion` returns `hold` for affected criteria.

## Run the public harness

Evaluator bundles are intentionally external. A custodian supplies an evaluator root whose files
match the public manifest digests; evaluator answers, seeded-defect locations, and reviewer rubrics
must never enter the repository, wheel, sdist, logs, prompts, or agent-visible outputs.

```console
python -m odibi_anchor.assurance.runner validate \
  --evaluator-root /custodian/evaluators
python -m odibi_anchor.assurance.runner execute \
  --evaluator-root /custodian/evaluators --matrix matrix.json --output retained/run-001
python -m odibi_anchor.assurance.runner score \
  --runs retained/candidate --baseline retained/baseline --output retained/comparison
```

Exit codes are 0 for a qualified harness outcome, 1 for an outcome failure, 2 for invalid or
incomplete evidence, and 3 for an unavailable harness or environment. Exit 3 is not a pass.
The output directory is explicit and append-only: the runner refuses to overwrite retained files.

## Read a scorecard

Every rate is a view over retained integer numerators and denominators:

| Metric | Numerator / denominator | Direction |
| --- | --- | --- |
| First-pass success | correct without retry or correction / completed scenarios | higher |
| Review rework | corrections and corrected scenarios / independently reviewed scenarios | lower |
| Unsupported claims | unsupported material claims / material claims sampled | lower |
| Escaped defects | evaluator-found escaped defects / detectable defects | lower; zero critical |
| False positives | unsupported emitted findings / emitted findings | lower |
| Unavailable evidence | unavailable applicable requirements / applicable requirements | monitor; never pass |

`not_applicable`, `unavailable`, and harness failure are distinct. An exclusion requires a retained
run ID and reason. Retries remain retained; first-attempt runs drive first-pass metrics. Reports put
critical failures before rates because a favorable total cannot offset a critical failure or a
required cohort regression. Ceremony reports median and p90 wall time, calls, prompts, evidence
items, and reviewer actions; it has no composite promotion score.

Assurance tier (`T0`–`T3`) and change scope (`localized` or `systemic`) are independent fields. The
manifest includes two scenario families in every Cartesian cell and tests each dimension without
inferring one from the other.

## Add a scenario without leakage

1. Create a materially new semantic family; do not clone a defect or evaluator rule across the
   development, qualification, and holdout splits.
2. Put only task-visible fixture metadata, explicit tier and scope, budgets, evaluator reference,
   and SHA-256 digests in `scenario_manifest.json`.
3. Keep the evaluator, expected result, rubric, holdout identity, and seeded location in the
   custodian-controlled evaluator root outside the repository and package.
4. Randomize benign details and include metamorphic or adversarial near-miss variants where useful.
5. Recompute the manifest digest, run source and clean-distribution tests, and scan tracked files,
   builds, logs, reports, and prompts for evaluator leakage.

A failed sealed holdout is not edited, dropped, relabeled, or repeatedly tuned. Correct and version
the product, then constitute a new independently owned holdout.

## Dogfood and independent review

Pre-register the predicted outcome, run baseline and candidate with the same fixture, budget, and
agent configuration in randomized order, compare raw records, classify every discrepancy, and
retain the evidence. Classifications are product defect, evaluator defect, documentation gap,
policy issue, test-data issue, host limitation, or agent misuse. An unavailable host or agent
family remains unavailable; never simulate it.

Independent reviewers receive the Spec, source and distribution digests, public manifest, blinded
run index, raw scorecards, exclusions, exceptions, leakage report, and packet—not implementer
conclusions or evaluator answers. Compliance here means conformance to the versioned Context
Workbench specification and policy, not certification against cited external standards.
