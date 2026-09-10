# Independent PR review

Use this reference for explicit pull-request, branch-comparison, or patch review when
the host does not provide a dedicated `reviewing-pull-requests` skill. Act as an
independent reviewer: review intent first and implementation second. Do not implement,
merge, push, vote, post comments, or modify source unless the owner separately
authorizes that action.

## Establish authority and boundary

Before reading proprietary evidence:

1. Classify the repository as work-authorized or personal. If the boundary is
   materially ambiguous, stop and ask; never mix records or evidence.
2. Select `work-pr-reviews` for work-authorized repositories or
   `personal-pr-reviews` for personal repositories when those Odibi Anchor
   managed projects are available. Create one bounded review work item per PR.
3. Record the repository, PR identifier or URL, target branch, source branch, exact
   target and source commit SHAs, intended behavior, non-goals, allowed verification,
   and prohibited effects. The checked-out repository remains authoritative; do not
   copy source into the managed project unnecessarily.
4. Treat credentials as capabilities, not evidence. Check token presence only; never
   print, persist, or include credentials in commands, artifacts, or replies.

Keep employer source, comments, implementation details, and review evidence inside the
work boundary. Never move them into personal projects, unrelated repositories, or
portable guidance. General review methodology may be portable; proprietary evidence
may not.

## Preserve the review posture

- Start read-only. Fetch remote refs and metadata only as needed to identify the exact
  review commits, and preserve all unrelated local work.
- Do not check out over local changes, reset, clean, stash, or alter source.
- Do not post comments, change votes, approve, merge, or push without separate explicit
  authorization for that external write.
- Stop and re-scope when target or source SHAs change. Never silently review a moving
  diff under old evidence.
- Passing tests, mergeability, or a Odibi Anchor gate does not establish that the
  implementation satisfies its intent.

## Gather evidence in decision order

1. Read the PR title, description, linked intent, relevant discussions, target and
   source refs, exact SHAs, status, and declared verification.
2. Inspect the exact target-to-source diff and changed-file summary before broad
   whole-file reads. Use the provider's merge-base semantics when appropriate.
3. Identify changed contracts, data flows, write paths, failure paths, and callers.
4. Read only the surrounding source, tests, schemas, and prior decisions needed to
   confirm or disconfirm a concrete risk.
5. Run the narrowest available verification that can change confidence. Separate
   static checks, deterministic tests, runtime evidence, and unavailable checks.
6. Reconfirm the exact target and source SHAs before issuing the recommendation.

Use prior reviews for calibration when requested or when a durable authorized baseline
exists. Distinguish observed team requirements, repository-local conventions,
owner-specific safeguards, and general engineering judgment. Absence of a prior comment
is not proof that behavior is accepted; an isolated preference is not a team standard.

## Apply the data-engineering lens

Scale depth to the change and examine:

1. **Scope and ownership:** Does the diff match intent and non-goals? Is the behavior in
   the right module? Is a new abstraction supported by concrete consumers?
2. **Data truth and contracts:** Confirm grain, authoritative sources, business rules,
   natural and merge keys, null and uniqueness behavior, schema, units, precision,
   timezones, defaults, and enum semantics.
3. **Movement and lineage:** Look for silent row loss or multiplication through filters,
   joins, casts, parsing, and deduplication. Ensure records can be traced to source and
   run context and that invalid records reach an explicit state.
4. **Reruns and writes:** Determine snapshot, append, merge, or replacement semantics.
   Test partial failure, retries, duplicate delivery, empty input, all-invalid batches,
   atomicity, recovery, and observed committed outcomes.
5. **Quality and operations:** Reconcile accepted, excluded, quarantined, invalid, and
   unaccounted rows. Ensure tolerant parsing does not create silent nulls and that
   quarantine, metrics, and errors support the actual operator.
6. **APIs, tests, and documentation:** Prefer simple typed contracts and behavioral
   tests. Cover grain uniqueness, malformed and duplicate inputs, retries, partial
   failures, schema evolution, and empty inputs without broad mock-driven confidence.

## Write independently evidenced findings

Prefer a few high-confidence behavioral, data-contract, security, operational, and test
findings over broad style commentary. Rank each finding:

- **Blocker / high:** credible incorrect data, silent loss or duplication, security
  exposure, destructive behavior, broken public contract, or unsafe production path.
- **Medium:** material latent failure, compatibility risk, or missing evidence that must
  be resolved before relying on the behavior.
- **Low / suggestion:** bounded maintainability or clarity improvement with no likely
  immediate correctness failure.
- **Nit:** non-behavioral preference; omit unless consistently enforced by the repository.

Each finding must include the observation and intended contract, concrete trigger or
reasoning path, consequence and affected party, exact evidence, smallest safe correction
or bounded question, confidence, unresolved assumptions, and disposition. Use `open
blocker`, `open`, `accepted risk`, `resolved`, or `superseded` dispositions. Be
question-led when the contract is ambiguous and direct when a defect is confirmed.

## Record and deliver the review

Update the bounded review record with repository and PR identity, exact target and
source SHAs, intent, non-goals, findings, verification provenance, finding dispositions,
unresolved assumptions, and final recommendation. Keep all calibration evidence in its
authorized boundary.

Use one recommendation:

- **Approve:** no open blockers; residual risks are explicit and acceptable.
- **Comment only:** questions or non-blocking improvements remain.
- **Request changes:** one or more open blockers remain.
- **Unable to conclude:** required evidence or authority is unavailable.

Lead the response with findings ordered by severity, followed by verification evidence
and unresolved assumptions. Do not bury blockers in process detail or claim runtime,
data, schema, policy, or post-write evidence that was not observed.

For re-review, compare the new exact diff against the previously reviewed source SHA,
retest only affected contracts plus necessary regressions, update every finding's
disposition, and preserve earlier review evidence rather than overwriting it.
