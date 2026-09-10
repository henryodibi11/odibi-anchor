# Runtime, guidance, and PR-readiness rollout

## Runtime order and compatibility

Assistant guidance uses 17 ordinary native packages under
`.assistant/skills/<skill>/SKILL.md`. Hosts configured for this Agent Skills root discover
these files directly. Odibi Anchor exposes the same names through `skills` and records a direct name through
`skill_loaded`; nested references and removed names are not accepted. Loaded names are
process-local bookkeeping and are never restored from historical records as authorization.

Prefer direct Python: `from odibi_anchor.bootstrap import init`, then `anchor, ROOT,
MANIFEST = init()`. It is the canonical dispatcher and preserves raw return shapes and
exceptions. For constrained environments use the stdlib CLI: `anchor help`, `anchor exec
ACTION '{}'`, `anchor batch FILE`, or `anchor shell`. `exec` is stateless across processes;
batch/shell boot once and retain process-local state. CLI stdout is deterministic
strict JSON and diagnostics are stderr.

Optional MCP stdio runs as `python -m odibi_anchor.mcp_server`.
`anchor_execute(action, args=None, response_version=1)` v1 remains the historical
unwrapped text representation: compact JSON for dictionaries and `str()` for other
result shapes; exceptions remain unwrapped. Explicit `response_version=2` is redacted
and enveloped. Callers may explicitly request `telemetry_version=1` only with
`response_version=2`. This adds bounded, request-local MCP server timing for gateway-lock
wait, lazy bootstrap, normalization/resolution, dispatch, response preparation,
base-envelope serialization, and connection cleanup. It also reports nested project-routing
reinitialization timing and the UTF-8 byte/token estimate of the canonical v2 envelope
without telemetry. Telemetry is returned on both successful and caught-error envelopes;
omitting it preserves the existing v1 and v2 contracts.

`server_total_ms` uses a monotonic clock from immediately before gateway-lock acquisition
through connection cleanup. It includes lock contention but excludes telemetry encoding,
FastMCP transport/framing, eager process startup and import warmup, scheduling, stdio, the
client harness, and network time. Stage values are diagnostics and routing reinitialization
is nested within response preparation, so those values must not be added together. A low
server total only locates delay outside this measured boundary; it does not by itself prove
network latency. Telemetry never contains request arguments, roots, credentials, response
content, tracebacks, table identifiers, or provider details.

HTTP requires `ANCHOR_EXPERIMENTAL_HTTP=1`, is disabled by default,
experimental/deprecated, and is unsafe for stateful multi-client use without isolation.

## Installed distribution roots

The wheel separates four paths that a source checkout can legitimately co-locate:

| Path | Installed behavior |
|---|---|
| Distribution resources | Immutable complete `.assistant` tree, sibling instructions, tool manifests, and executable tool modules beside the installed package |
| Odibi Anchor home | Writable active-project selection, managed projects, session state, and default memory |
| Artifact root | Durable records beneath the selected managed project |
| Target root | The external repository selected by a project descriptor or `init(root=...)` |

Writable-home precedence is exact: `ANCHOR_HOME`, then the active environment profile's
`anchor_root`, then the verified repository root for source/editable execution, then the
operating-system user-state fallback. On Unix the fallback is
`$XDG_STATE_HOME/odibi-anchor` when `XDG_STATE_HOME` is configured and
`~/.local/state/odibi-anchor` otherwise. Windows uses local application state.
Profile `home` remains the user's workspace home and does not select Odibi Anchor
state. `ANCHOR_HOME` wins over profile `anchor_root`; `ANCHOR_MEMORY_DB` remains the highest-precedence
memory override, followed by an explicit profile memory path and then
`<anchor_home>/.agent_memory.db`.

`init(root=...)` is only a target override. In installed mode neither cwd nor the target
becomes the Odibi Anchor home or default memory location, and runtime state is never
written beneath site-packages. Source/editable execution in the repository continues to
use the repository as home and to discover its repository-owned resources. Environment
profiles may still select their documented `anchor_root`, `skills_dir`, and memory paths.

Portable direct-orb MCP mode requires an absolute existing `ANCHOR_PROJECT_ROOT`. Before
initializing the dispatcher it recomputes installed runtime paths, rejects empty or unsafe
explicit state/profile values, requires state and memory outside both target and distribution
resources, and resolves an immutable managed-project binding. Set `ANCHOR_PROJECT_ID` to require
one exact registry entry; without it, the target must match exactly one entry. A remembered
`.active_project` is only an interactive preference and is ignored by server routing unless
the temporary `ANCHOR_ALLOW_LEGACY_SELECTOR=1` compatibility mode is explicitly enabled. The
effective dispatcher root must equal the canonical requested checkout. These checks perform
no writes; ordinary initialization creates state only after they pass.

## Concurrent-project operations

The supported concurrency boundary is multiple live runtimes bound to **distinct managed
projects** under one `ANCHOR_HOME`. It is not same-project source editing or distributed consensus.
Same-project conflicting writers remain unsupported and must fail closed.

Inspect the current runtime before rollout or recovery:

```python
diagnostics = anchor("concurrency", command="inspect")
plan = anchor("concurrency", command="dry_run")
```

Both calls are strictly read-only, including against uninitialized state. They report the
immutable route and legacy-selector use, continuity owner/generation and preserved legacy state,
learning schema/quarantined obligations/exact owner, selection-recovery schema and separately
labelled lifecycle states, and the five-second SQLite contention contract. The neutral
`selected_never_applied` total is not a leak count.

Migration domains are `route`, `continuity`, `learning`, and `selection_recovery`. Apply uses the
existing deterministic initializers and is safe to rerun:

```python
applied = anchor("concurrency", command="apply", domains=[
    "route", "continuity", "learning", "selection_recovery",
])
```

The response always says `atomic: false`: these are independent domains, not one transaction.
Route has no destructive persisted migration. Continuity adoption is additive and preserves a
content-addressed backup of ambiguous legacy state. A mismatch blocks the remaining domains and
the concurrency support claim.

Rollback requires exactly one domain. Route and continuity report unavailable rather than delete
identity or evidence. Learning requires its verified backup, marker, unchanged rollback digest,
and zero incompatible writes. Selection recovery is eligible only before any immutable
abandonment/recovery event and while its verified pre-v3 projection is unchanged.

```python
rollback = anchor("concurrency", command="rollback", domains=["selection_recovery"])
```

An ineligible rollback returns a manual-recovery packet. Preserve the database, backups,
continuity files, immutable events, diagnostic output, and runtime version; stop writes for the
affected project boundary; use a separate per-project `ANCHOR_HOME` for temporary containment; and
escalate. Never delete or rewrite history to make a rollback appear eligible.

## Crash and abandoned-selection runbook

1. Run `anchor("concurrency", command="inspect")`. Confirm the exact project, target, artifact root,
   runtime ID, binding source, and schema checksums. A stale or ambiguous binding requires process
   restart/rebootstrap before mutation.
2. Run `anchor("task_rebind")` first. It rebinds only the latest open task matching the immutable
   route/session authority; it accepts no caller-supplied task ID.
3. In a current accepted exact-owner task, inspect selections with
   `anchor("memory", "recovery", action="inspect")`. Treat `active_pending`,
   `terminal_unresolved`, `abandoned`, `recovered`, `semantically_disposed`, and `ambiguous` as
   different states.
4. Declare abandonment only for one exact selection whose originating exact-owner task has a
   durable `blocked`, `failed`, or `superseded` closure. Supply bounded structured `reason` and
   `evidence`. Open/completed/missing/foreign authority must be denied.
5. Recover the immutable abandonment into the current exact-owner task. Repeating the same request
   is idempotent; conflicting replay fails closed. The original selection and event rows remain.
6. Separately record the semantic application or disposition after recovery. Abandonment never
   implies `irrelevant`, and no command may auto-write or bulk-write that disposition.

For quarantined learning obligations, preserve the row and migration evidence and correct the
owner through an approved migration; never use a global active/latest query. For a
`PersistenceContentionError`, retain `operation`, `owner_key_kind`, `duration_ms`, `timeout_ms`, and
`retryable`; inspect ownership before retrying. For malformed continuity, checksum drift, false
abandonment, or a failed migration, stop and use the manual-recovery packet rather than guessing.

## Concurrency release gate

Enable the claim separately for each host profile only when all of these are retained:

- the complete WI-2026-0007 real two-process matrix is repeatably green;
- focused legacy, false-abandonment, governed-recovery, migration rerun, and rollback tests pass;
- a clean wheel installs in isolation and its MCP stdio runtime exposes the same diagnostics;
- the exact host profile has qualification evidence and version/digest provenance.

This change qualifies orb/Linux only when its executed suite is green. Local Windows and direct
Python Databricks remain unqualified until the same host-specific matrix is executed. Reverse or
contain the rollout on cross-project evidence, false abandonment, fabricated semantic disposition,
unresolved migration mismatch, repeated lock timeouts beyond the fixed bound, or nondeterministic
qualification. New servers default to explicit binding; measured legacy fallback is temporary and
must not be interpreted as concurrency authority.

### Private release transport

Governed orb setup installs only a clean-built private wheel whose version, source commit,
SHA-256 digest, and compatibility tuple match an independently reviewed project release pin.
A private GitHub release asset is authenticated transport, not immutable authority: setup must
download it through `gh` using repository-scoped GitHub App access, verify the pinned digest
before `pip install`, and fail clearly if either download or verification is unavailable. Do not
commit credentials, token-bearing URLs, or a checksum sourced only from the replaceable release
asset. Creating the tag or release remains a separate exact-state publication decision after
release qualification.

The runtime does not migrate, copy, or delete state written by an older installation.
Use an explicit `ANCHOR_HOME` to reopen such a location. Non-inline session notebooks import
`odibi_anchor.bootstrap.init` and restore their recorded project and target; they do
not read `agent_init.py` from the home or target.

## Guidance compatibility

The current discovery and runtime surface contains exactly 17 native skills. Each is a
direct directory under `.assistant/skills`; there is no alias registry, redirect, or
pack/recipe resolution. `skill_loaded` records only one current direct name and remains
bookkeeping rather than attestation.

Earlier internal scaffolding grouped guidance into eight broad packs and exposed recipe
names as top-level calls. The 0.9 simplification moved their content into the 14 direct
owners or non-discoverable references. Calls using those removed identities now fail and
must be changed to the applicable current owner; no external deprecation period is implied.

Official Claude Code discovery uses `.claude/skills`, not the canonical `.assistant`
source root. A separately approved Claude qualification must copy all 17 complete
canonical skill directories byte-for-byte into `.claude/skills`; it may not curate,
diverge, or create a second taxonomy or behavior authority.

## Local PR readiness

Configuration is exact-schema UTF-8 JSON at
`<artifact_root>/.odibi-anchor/pr.json`. An absent file uses the defaults below;
when the file exists, unknown or missing keys and wrong types are rejected.
Runtime overrides are tightening-only: schema, provider, and target ref are fixed;
deny globs can only be added; required booleans/readiness can only become stricter;
line length can only decrease; and title prefixes can only select configured values.

```json
{
  "schema_version": 1,
  "provider": "azure_devops",
  "pr_readiness": "recommended",
  "default_target_ref": "main",
  "title_prefixes": ["feat", "fix", "refactor", "docs", "chore", "test"],
  "work_item_required": false,
  "live_validation_required": false,
  "deny_globs": ["*.env", "*.pem", "*.key", ".agent_memory.db"],
  "max_changed_line_length": 120
}
```

Readiness records only local branch, HEAD/target/merge-base SHAs, exact local diffs,
worktree/index state, and sourced UTC attestations. It never fetches or pushes, updates
a ref, changes the worktree/index, contacts a provider, or claims remote Azure DevOps
validation. Local Git analysis may create unreachable objects while calculating tree
or conflict evidence. The provider field
controls draft conventions only: no PR/work-item creation, reviewer assignment, push,
or live conflict claim occurs. Live and subjective checks remain unknown without the
required target/source/time attestation.

Drafts are atomically written beneath
`pull_requests/<collision-resistant-branch-directory>/PR_DRAFT.md`; the sanitized
branch slug includes a digest. Drafts are managed artifacts, separate from source and
evidence ledgers, and neither create PR applicability nor stale their own snapshot.

At a final checkpoint, preflight, tests, the fully evaluated deferred gate, and learn
input validation run first. After they succeed, a fresh snapshot is evaluated and any
required draft is persisted. Durable learning is published as the final draft
transaction step; checkpoint success/baseline bookkeeping commits afterward.
`generate_pr_draft` is tri-state: `True` requests generation, `False` may suppress a
recommendation but never a requirement, and `None` follows policy when policy is
evaluated. Repository policy is loaded at a final checkpoint, for an explicit
`generate_pr_draft=True`, or for a stored task-level `True`; a non-final `None` does
not discover repository-required policy. Required draft
failure leaves prior checkpoint bookkeeping unchanged and reports core success
separately. Non-final checkpoints do not infer finality.

Azure DevOps users may copy the local draft into their provider workflow, but must
perform authentication, remote freshness/conflict checks, work-item links, reviewer
policy, and PR creation outside odibi-anchor. Those operations are out of scope.
