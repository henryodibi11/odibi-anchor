---
name: setting-up-odibi-anchor
description: Installs, configures, diagnoses, upgrades, or recovers Odibi Anchor across local, Amp, Claude, ChatGPT, and Databricks hosts; do not use for ordinary work after a healthy Anchor runtime is already bound.
---

# Setting up Odibi Anchor

Establish one verified path from package installation to an exact managed-project runtime.

## When to load

Load when Anchor is missing, stale, unconfigured, bound to the wrong project, recovering
after compute replacement, or being installed for a new host or repository.

## When NOT to load

Do not load for ordinary implementation, review, data work, or memory use after the managed
launcher returns a ready startup packet.

## Workflow

1. Identify the exact intended project ID and instruction root. For an existing configured
   project, run the sibling `.assistant/agent_bootstrap.py` with only
   `init_globals={"ANCHOR_PROJECT_ID": "<project-id>"}`. Do not rediscover host, local state,
   durable state, authority, target, or portfolio paths manually.
2. The managed Databricks launcher checks PyPI before importing Anchor. If the distribution is
   missing or stale, execute its exact pinned latest-stable `%pip install` remediation, run
   `dbutils.library.restartPython()`, and rerun the same launcher call. Never guess a version or
   use an unrecorded moving install. The launcher verifies distribution/runtime agreement.
3. Inspect `STARTUP_PACKET`: require `status=ready`, the requested project ID and exact target,
   then use its `managed_artifact_actions` for artifact discovery instead of direct traversal.
4. If no portfolio exists, create a launch-ready PortfolioV1 without manual TOML surgery:

   ```bash
   anchor portfolio scaffold \
     --config <absolute-config-path> \
     --host <host-id> --adapter <adapter> \
     --target-root <absolute-project-root> --project <project-id> \
     --authority <authority-id> --local-state-root <absolute-local-state-root> \
     --instruction-root <absolute-instruction-root> \
     [--durable-root <absolute-durable-root>]
   ```

   If the portfolio exists but the explicitly approved project does not, prefer the managed
   launcher with `ANCHOR_CREATE_PROJECT=True` and one exact existing `ANCHOR_PROJECT_ROOT`.
   It performs the guarded portfolio update, scaffold/register, and durable checkpoint. The
   target need not be Git unless source-change evidence is required. A request merely to use a
   project does not authorize this write-capable path.

5. Run `anchor portfolio validate`, then `anchor setup-host <adapter> --target
   <instruction-root>`. Setup is manifest-managed and must refuse modified managed files
   or incompatible user-owned collisions.
6. The managed launcher is the primary preparation path. Use the direct preparation API below
   only to recover or diagnose a launcher failure; do not substitute a prohibited CLI subprocess:

   ```python
   import os
   import runpy
   from odibi_anchor import prepare_portfolio_runtime

   prepared = prepare_portfolio_runtime(
       config_path="<absolute-portfolio-path>",
       host_id="<host-id>",
       project_id="<project-id>",
   )
   os.environ.update(prepared["environment"])
   namespace = runpy.run_path(prepared["next_operation"]["arguments"]["script"])
   anchor = namespace["anchor"]
   ```

   Do not set `ANCHOR_HOME` manually, map `ANCHOR_DURABLE_ROOT` to it, or probe the durable
   Volume through FUSE. Preparation keeps live state on local compute and restores immutable
   snapshots through the Databricks Files API. Apply every returned environment field exactly.
   On shared/serverless compute, configure a stable user-specific local state path rather than
   a generic `/tmp/odibi-anchor` path that another OS user can own. A verified v2 restore safely
   relocates continuity records when that configured local path changes.
   Other hosts may use `anchor portfolio prepare --config <path> --host <id> --project <id>`.
   The `anchor` callable comes from the launcher namespace or `launch()` return value; never
   attempt `from odibi_anchor import anchor`.
7. Run doctor after applying the prepared environment and follow any remediation. If doctor
   is run earlier on unconfigured Databricks, follow its `portfolio.prepare` operation rather
   than inventing route or state paths.
8. Verify the immutable route, orientation, and task lifecycle before substantive work.
   Accept the task before loading task-specific skills; then load every skill named in the
   accepted task result with `anchor("skill_loaded", "<name>")`. `skill_loaded` requires the
   active task by design, so calling it before task acceptance is an invocation error, not a
   circular dependency.

## Host boundaries

- **Databricks/Genie:** keep live SQLite and managed project artifacts under `/tmp`; use a UC
  Volume only for immutable database-and-artifact snapshots through the Workspace Files API.
  Use the Databricks extra. After compute replacement, rerun portfolio preparation to restore
  both before launch. Never run concurrent
  writers unless the actual filesystem/runtime has been qualified. The preferred launcher
  automatically attaches read-only Git Folder identity when the Workspace API can attest the
  target. For source changes, retain implementation mode, declare exact repository scope, and
  explicitly acknowledge unknown local Git state; do not downgrade source work to planning.
- **Amp:** install the MCP extra when using the stdio server. Keep the virtual environment
  and `ANCHOR_HOME` outside ephemeral build output.
- **Claude:** install guidance at the host's instruction root and preserve user-owned
  `CLAUDE.md` content when it already delegates to `.assistant_instructions.md`.
- **ChatGPT:** use only the execution and persistence capabilities actually available;
  do not claim durable or stateful operation from a guidance-only attachment.

## Recovery

For absent local Databricks state, rerun portfolio preparation before doctor or bootstrap rather
than copying SQLite manually. For modified managed guidance, inspect the collision and preserve
the user's file; never delete staging, legacy, state, or snapshot directories by pattern.
Cleanup requires an exact inventory and classification as active, evidence, rollback, or
disposable.

## Related skills

Use [[dependency-management]] when changing package dependency policy. Use
[[debugging]] only when a verified setup path fails because of a product defect.

## Enforcement

Self-enforced. Portfolio validation, immutable routing, host manifests, authority checks,
and lifecycle gates enforce the underlying runtime boundaries.
