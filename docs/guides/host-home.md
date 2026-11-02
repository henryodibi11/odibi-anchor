# Host home and safe migration (0.4.1)

Host installation is distinct from project/runtime state. `setup_host(target, adapter=...)`
defaults to `<target>/anchor`. Choose an absolute folder name/location with `home=...`, CLI
`--home`, or `ANCHOR_HOST_HOME`, in that precedence order. The recorded instruction pointer
remembers the selection, so later setup/startup does not depend on CWD or require an exported
variable. A conflicting selection is refused, not silently adopted.

`ANCHOR_HOME` keeps its existing meaning: live runtime state. Portfolio `local_state_root`,
durable roots, project targets, artifact namespaces, SQLite ownership, and concurrency rules
are unchanged. On Databricks the host home must be durable `/Workspace` storage, while live
state remains compute-local. Do not point `ANCHOR_HOME` at the host home to relocate it.

## What moves, and what stays

| File or content | Location after setup |
| --- | --- |
| Canonical managed bootstrap | `<host-home>/agent_bootstrap.py` |
| Portfolio | `<host-home>/anchor.toml` |
| Host identity and binding | `<host-home>/host.json`, `.odibi-anchor-host-binding.json` |
| Guidance ownership hashes | `<host-home>/guidance.json` |
| Operating contract | `<host-home>/instructions.md` |
| Backups, journal, last rollback receipt | `<host-home>/backups/`, `pending.json`, `last-migration.json` |
| Native instruction entry points | Existing instruction root (`AGENTS.md` or `CLAUDE.md`) |
| Instruction discovery pointer | `<instruction-root>/.assistant_instructions.md` |
| Guidance resources and native skills | Existing `.assistant/` resources and Claude `.claude/skills/` |
| Project artifacts, runtime database, durable snapshots | Their existing configured owners/locations |

Amp/ChatGPT and Claude need native instruction discovery files; Claude also discovers native
skills under `.claude/skills`. The `.assistant_instructions.md` pointer preserves existing host
integrations. `.assistant` references and skills remain for compatibility; neither a bootstrap
nor the host configuration/ownership manifest belongs there. Databricks does not require a
root `agent_bootstrap.py`: it was a source-only launcher, not a platform entry point. The
source repository and distribution still contain their separate source/managed launcher
resources; installation publishes only the managed one.

## Plan, apply, and recover

For example, with absolute paths chosen by the owner:

```bash
anchor setup-host amp --target /host/instructions --home /owner/tools/anchor --reconcile
anchor setup-host amp --target /host/instructions --home /owner/tools/anchor --reconcile --apply
```

Python and Databricks use the same options:

```python
from odibi_anchor import setup_host

plan = setup_host(instruction_root, adapter="databricks", home=host_home, reconcile=True)
# Inspect plan["files"], changed_files and approval_required before applying.
result = setup_host(instruction_root, adapter="databricks", home=host_home,
                    reconcile=True, dry_run=False)
```

Unknown/edited files are never replaced by ordinary setup. Only an owner-approved
`--approve-replace-edited` / `approve_replace_edited=True` reconciliation replaces them,
after saving verified backups. Conflicting portfolios always require an explicit owner
choice. An explicitly supplied external portfolio is copied, not deleted; the legacy default
`<instruction-root>/.odibi-anchor/anchor.toml` is relocated after validation. Its contents and
logical state roots are preserved byte-for-byte. Other content under `.odibi-anchor` stays put.

The old root and `.assistant` bootstraps and root ownership metadata are removed only when
their ownership can be established. Receipt-proven legacy backups move into
`<host-home>/backups/legacy`; unknown files and empty legacy directories are retained, never
recursively deleted. An existing legacy runtime verifies its old installation until explicit
setup migrates it; ordinary bootstrap does not surprise-migrate a live configuration.

All changed bytes have readback-verified backups and a persistent journal before publication.
An interrupted transaction refuses further setup/bootstrap with `host_migration_pending`.
A temporary `.anchor-host-migration.json` locator at the instruction root makes that refusal
discoverable to a surviving legacy launcher even when the chosen home is elsewhere. It contains
only the home/owner/journal digest, is created before the journal, and is removed last after
publication or rollback; permanent metadata and saved bytes stay in the chosen home.
After the interrupted process is stopped and any in-flight Workspace request has finished:

```bash
anchor setup-host amp --target /host/instructions --home /owner/tools/anchor --rollback
```

This also rolls back the most recent successful publication. Rollback checks both the saved
bytes and current destinations and refuses concurrent edits rather than overwriting them.
If the Workspace mutation timed out, its outcome may still be unknown: do not race it with a
rollback. Preserve the journal/backups, establish request completion, then recover. No new
distributed locking or state/concurrency design is introduced.

Bootstrap uses the path returned in `result["launcher"]`; for the default home:

```python
namespace = runpy.run_path(f"{instruction_root}/anchor/agent_bootstrap.py",
                          init_globals={"ANCHOR_PROJECT_ID": project_id})
```

The launcher validates that its host identity and the instruction root's pointer agree. A
copied or incomplete home cannot silently route to another host. Startup/doctor report the
same canonical path. Existing exact version pins, bounded Workspace IO, bootstrap phase
timings, and metadata-validated guidance receipts remain in effect. Receipt caches remain
private and compute-local because they attest that compute's verification, not durable state.

Validation in an orb is not a live Databricks rollout. Workspace SDK simulations and
installed-wheel tests cover migration, receipt invalidation, conflict and crash behavior;
real Workspace/FUSE behavior and Windows execution need their own host evidence.
