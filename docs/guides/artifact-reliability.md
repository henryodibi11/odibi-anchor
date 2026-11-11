# Saved artifact discovery, readiness and safe updates

Artifact inspection is project-scoped and does not require task admission. Empty host receipts are not proof that a saved record is absent. These APIs reuse the registry exposed by `project status`; they do not create another catalogue, session or approval store.

## Find and read saved records without bootstrap

Use an existing Anchor home and exact registered project ID:

```bash
anchor artifact-schema
anchor artifacts --home /absolute/anchor-home --project example
anchor artifacts --home /absolute/anchor-home --project example --path specs/EXPORT_SPEC.md
anchor artifacts --home /absolute/anchor-home --project example --path specs/EXPORT_SPEC.md --view human
anchor readiness work_item WI-2026-0001 --home /absolute/anchor-home --project example --rigor compact --spec EXPORT
anchor readiness problem PRB-2026-0001 --home /absolute/anchor-home --project example --stage recommendation
anchor readiness spec EXPORT --home /absolute/anchor-home --project example
```

The CLI returns JSON (`ok`, `result` or `error`). Human view returns the same saved page as a string inside `result`, not a generated summary. Python callers use `odibi_anchor.artifacts.inspect_artifacts`, `inspect_readiness` and `inspect_artifact_contracts`. Inspection does not change the active-project selector, database, session, artifact bytes or modification times. Discovery grants no write authority.

```python
from odibi_anchor.artifacts import inspect_artifacts

page = inspect_artifacts(anchor_home="/absolute/anchor-home", project_id="example",
                         path="specs/EXPORT_SPEC.md", limit=10000)
if page["next_offset"] is not None:
    next_page = inspect_artifacts(anchor_home="/absolute/anchor-home", project_id="example",
                                  path=page["relative_path"], offset=page["next_offset"],
                                  expected_sha256=page["content_sha256"], limit=10000)
```

Inventory pages contain at most 200 entries. UTF-8 pages contain at most 32000 Unicode characters, preserve CRLF and cover the complete saved content when concatenated. Follow `next_offset`; later content pages require `expected_sha256`. A changed record refuses the page rather than joining two versions. Inventory is point-in-time, not a filesystem snapshot. Binary artifacts remain discoverable but text decoding fails explicitly. Missing projects/files, unsafe paths, unreadable bytes and stale pages are errors, not empty records. Paths must be exact, within declared roots and free of symlink/reparse traversal.

In an already authorized runtime, use `project artifacts`, `project read_artifact`, native `work_item show`, `problem show` or `spec show`. `spec show` accepts an exact canonical name or `*_SPEC.md` filename; it never chooses the first substring match. Historical `spec status` matching remains unchanged. Malformed records can still be read through `project read_artifact` even if their typed native reader refuses to parse them. Decisions, PROJECT.md, notebooks and retained canonical handoffs use that same generic read path.

## Three different identities

- `artifact_id` binds project ID, artifact root and relative path. Same local record IDs in different projects are different artifacts. Moving a home/root changes this observation identity; it is not a portable authorization token.
- `content_sha256` hashes all exact saved bytes. Operational ledger/revision changes alter it. This is the update/pagination precondition, not semantic quality or authenticated host evidence.
- Work-item `fingerprint` retains its existing normalized material semantics. Review-ledger writes do not invalidate it. Plan `binding_sha256` additionally binds project/artifact, dependencies and the exact linked spec hash. Observational revision/hash fields in a subject are not part of this material binding.

## Pure readiness is not certification

`work_item check_plan` and `request_plan_approval` use the same evaluator, before owner contact and again after an owner response. Default `readiness_version=2` accounts for saved plan context, scope, non-goals, acceptance criteria, implementation approach, validation intent, risks and dependencies. Incomplete drafts remain creatable/updatable. `Pending: reason; next: action` is addressed for review but not ready for approval; `N/A: reason` is an explained disposition. Plain `TBD`, `Pending` or `N/A` is not addressed content. A populated field is not proof of adequacy: `semantic_certification` is always false.

The checker separates content addressed for review, content ready for approval, review/rigor/dependency sufficiency and execution admission. It does not demand implemented tests, future files or a real PR to review a plan. Plan readiness does not grant execution authority. Workflow `check_plan`/`accept_plan` still owns implementation admission, and standalone workflows remain supported.

Problem `check_readiness` supports framing, recommendation traceability and close (critical-hypothesis disposition). `link_spec` and `close` reuse those evaluators. Spec `check_plan` inspects saved success criteria, not existence of proposed files. Historical spec `validate` checks implementation assumptions and `review` **writes** a Review section; neither is a substitute for pure plan inspection.

## Safe writes and migration

Discover native fields/table columns from `anchor artifact-schema` or `project status` → `artifact_contract.native_contracts`; definitions are derived from native writers and the managed schema registry. Action help for `problem`, `work_item` and `spec` includes the same saved-record contract without bootstrap. Text fields take strings; table fields take lists of row objects with the advertised column names. Work-item help also identifies non-text material types, so callers need not infer lists from narrative field names. Unknown fields, unknown table cells and conflicting aliases are rejected rather than silently discarded.

Use strict writes for new consumers:

```python
saved = anchor("work_item", "show", "WI-2026-0001", output_format="dict")
updated = anchor("work_item", "update", "WI-2026-0001", scope="Revised bounded scope",
                 write_version=2, expected_sha256=saved["content_sha256"], output_format="dict")
```

Work-item and Problem updates also accept `expected_revision` (a positive integer). Strict Problem fields/cells reject unsupported types rather than coercing them. Row patches preserve omitted cells; an explicit empty string clears a cell. Native writes verify that the candidate can round-trip. Strict updates refuse a saved record with custom content the native renderer would lose; `allow_destructive=True` explicitly permits that rewrite after binding the current saved version. Prefer preserving/reconciling the content instead. Strict prose must not introduce reserved native section boundaries.

Specs use full replacement, never an implicit merge:

```python
saved = anchor("spec", "show", "EXPORT", output_format="dict")
preview = anchor("spec", "preview_update", "EXPORT", content=new_markdown,
                 write_version=2, expected_sha256=saved["content_sha256"], replacement=True,
                 output_format="dict")
updated = anchor("spec", "update", "EXPORT", content=new_markdown,
                 write_version=2, expected_sha256=saved["content_sha256"], replacement=True,
                 allow_destructive=True, output_format="dict")
```

Preview reports removed headings without writing. Section removal requires explicit destructive permission with the expected hash and replacement flag; this is a supported redesign path, not a rule that old headings must live forever. Same-heading body edits are replacements too: inspect the preview/candidate, not only the heading list.

Compatibility: `write_version=1` remains the default for historical callers, with warnings about legacy coercion/preconditions; use version 2 for strict concurrency/no-loss guarantees. Unknown arguments are rejected in both modes. `readiness_version=1` explicitly preserves the historical review/dependency approval gate without requiring addressed plan content; default approval readiness is now version 2. These versions are independent of MCP response versions 1/2. No persisted-record migration or bulk rewrite is required. Existing fingerprint-only reviews remain identifiable as `material_only_legacy`, not retroactively authenticated or spec-bound.

Writes use the existing best-effort saved-byte compare and atomic replacement. This is optimistic stale-version protection, not a filesystem lock or a multi-record transaction. If a grouped approval or multi-artifact operation fails after a write, reconcile each saved record before retrying; never infer rollback from a transport error.

## Review subjects and approval projections

Get a `work_item review_subject` packet before reviewing the saved plan. Submit `subject_type="plan"`, `write_version=2`, `expected_subject=packet["subject"]` and the current `expected_fingerprint`. Foreign projects, changed material/dependencies or changed linked spec bytes refuse the review. Ledger-only changes do not stale a material review. `reviewer_ref` is a caller claim; `reviewer_authentication="none"` and host observations remain separate.

Implementation review uses the existing workflow `bind_review`/`review` operations after a candidate exists, bound to workflow plan and candidate digests. It cannot substitute for a work-item plan review. The implementation review result carries `subject_type="implementation"`; the strict workflow task-binding schema is unchanged.

The legacy `plan_approval.approved` boolean still means only material/dependency approval. The additive `approval_projection` exposes current project/material/spec approval, content disposition and a separate execution-admission explanation. Use `check_plan` for current rigor/review readiness and workflow admission for actual execution. Neither caller-supplied reviewer strings nor populated content can manufacture owner authorization.

## Existing recovery contracts

`snapshot`/`save_snap`/`load_snap`, canonical handoff validation and exact-task continuity are unchanged. `load_snap` decodes saved JSON; that alone is not freshness/authority validation. Generic artifact readback can inspect a retained canonical handoff's exact saved bytes without adopting its authority. Host history, source/task continuity, durable restore and lifecycle readiness remain separate contracts.
