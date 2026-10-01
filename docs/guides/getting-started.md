# Getting started

Odibi Anchor separates installation, host configuration, project routing, and runtime
bootstrap. Completing one step does not silently perform the others.

## Choose an installation

Install one exact release:

```bash
# Local Python or CLI
python -m pip install "odibi-anchor==<version>"

# Amp or another MCP host
python -m pip install "odibi-anchor[mcp]==<version>"
```

In Databricks use the qualified SDK extra and restart Python:

```python
%pip install "odibi-anchor[databricks]==<version>"
dbutils.library.restartPython()
```

Confirm the distribution and runtime agree:

```python
import importlib.metadata
import pathlib
import odibi_anchor

assert importlib.metadata.version("odibi-anchor") == odibi_anchor.__version__
print(odibi_anchor.__version__)
print(pathlib.Path(odibi_anchor.__file__).resolve())
```

## Databricks: one-time personal-workspace setup

This setup keeps three responsibilities separate:

- host guidance and the portfolio are persistent Workspace files under your user directory;
- live SQLite state and managed working artifacts use user-specific local compute storage;
- immutable recovery snapshots use a Unity Catalog Volume.

Do not copy a portfolio, database, memories, snapshots, or managed project artifacts from another
authority or workspace. Create a fresh authority for the personal workspace.

### 1. Create durable snapshot storage

In a SQL cell, create a Volume in a catalog and schema you own. Replace
`workspace.default` if your personal workspace uses a different namespace:

```sql
CREATE VOLUME IF NOT EXISTS workspace.default.odibi_anchor_state;
```

The corresponding Files API path is
`/Volumes/workspace/default/odibi_anchor_state`. Anchor stores only immutable recovery snapshots
there; it never runs the live SQLite database from the Volume.

### 2. Install host guidance and create the first project

Run this in a Python cell after installing the Databricks extra and restarting Python:

```python
from databricks.sdk import WorkspaceClient
from odibi_anchor import scaffold_portfolio, setup_host

user = spark.sql("SELECT current_user()").first()[0]
instruction_root = f"/Workspace/Users/{user}"
project_id = "personal-work"
project_root = f"{instruction_root}/projects/{project_id}"
config_path = f"{instruction_root}/.odibi-anchor/anchor.toml"

workspace = WorkspaceClient().workspace
workspace.mkdirs(f"/Users/{user}/projects/{project_id}")

guidance = setup_host(instruction_root, adapter="databricks")

portfolio = scaffold_portfolio(
    config_path,
    host_id="databricks-personal",
    adapter="databricks",
    project_id=project_id,
    target_root=project_root,
    authority_id="personal-owner",
    local_state_root=f"/tmp/odibi-anchor-{user}",
    instruction_root=instruction_root,
    durable_root="/Volumes/workspace/default/odibi_anchor_state",
)
assert portfolio["validation"]["status"] == "valid", portfolio
```

`scaffold_portfolio` refuses to overwrite an existing portfolio. Keep this file at
`<instruction_root>/.odibi-anchor/anchor.toml`: that is the managed launcher's default persistent
location. `local_state_root` is a stable logical base: on shared/serverless compute the launcher
uses a physical root isolated by effective UID and OS-account fingerprint, then restores verified
v2 state when the compute identity changes. Do not rewrite the portfolio with each observed UID.
A plain Workspace directory is sufficient for managed-artifact work. Choose the task's
`execution_mode` by its deliverable, not by whether `repository_evidence` is present:

| Deliverable | Execution mode | Repository requirement |
| --- | --- | --- |
| Inspect, analyze, review | `read_only` | None; no source baseline or provider calls |
| Only Anchor-managed artifacts | `artifact_only` | None; arbitrary target files are not artifacts |
| Target source files | `source_change` | Canonical local Git, or an attested Databricks Git Folder |
| Tables or other data | `data_change` | Separate data policy; does not grant source authority |

For managed artifacts in a plain folder, after bootstrap and session creation:

```python
task = anchor("task", "<description>", goal="<goal>", mode="implementation",
              work_type="change", execution_mode="artifact_only",
              acceptance_criteria=["<completion check>"])
```

`artifact_only` cannot write target source. Gate rejects non-artifact target changes even
when the legacy mode is `implementation`. Do not relabel source edits as artifacts or switch
to documentation mode to bypass this boundary.

For source edits, use an existing or explicitly approved clone at its canonical local Git
worktree root with a clean task-start worktree. Alternatively, use a Databricks Git Folder
and supply an explicit `repository_provider` at initialization, then:

```python
task = anchor("task", "<description>", goal="<goal>", mode="implementation",
              work_type="change", execution_mode="source_change",
              repository_scope=["<relative-source-path>"], accept_unknown_git_state=True,
              acceptance_criteria=["<completion check>"])
```

The Git Folder route captures host identity and scoped task-start bytes, **not** local Git
cleanliness, history, merge-base, or PR readiness. A provider that rejects a plain Workspace
`DIRECTORY` does not grant source authority. If neither source route is available, stop and ask
the owner; use artifacts only when they actually meet the goal. Git Folder conversion is only
needed for source changes, not managed-artifact deliverables. No automatic conversion occurs.

Every accepted task returns `source_authority` with `status`, `reason`, `capabilities`, and
concrete `guidance`. Non-source modes report `not_requested` without probing Git or providers;
this is not a claim that source authority is available or unavailable. Source tasks report
`available_local_git` or `available_databricks_git_folder` only after baseline capture.
Local Git `pr_readiness: not_verified` requires separate verification; it is not a passed check.
Plain-folder source tasks remain blocked, with the managed-artifact invocation in the error.

### 3. Resolve an unmanaged-guidance collision safely

`HostSetupError: unmanaged destination collision: <path>` means setup found a file it does not own.
It stops before publication rather than overwriting that file. Choose one of these routes; never
delete an unknown instruction tree merely to make setup pass.

**Keep the existing guidance:** install Anchor under a dedicated instruction root, and use that
same root for `config_path`, `instruction_root`, and the launcher:

```python
workspace = WorkspaceClient().workspace
workspace.mkdirs(f"/Users/{user}/odibi-anchor-host")
instruction_root = f"/Workspace/Users/{user}/odibi-anchor-host"
config_path = f"{instruction_root}/.odibi-anchor/anchor.toml"
guidance = setup_host(instruction_root, adapter="databricks")
```

Then run the portfolio scaffold from step 2 with these updated variables.

**Replace confirmed obsolete guidance:** after inspecting and confirming that all three paths are
from an installation you no longer need, remove only those exact paths through the Workspace API:

```python
from databricks.sdk.errors import NotFound

api_root = f"/Users/{user}"
for relative in [".assistant", ".assistant_instructions.md", "agent_bootstrap.py"]:
    try:
        workspace.delete(f"{api_root}/{relative}", recursive=True)
    except NotFound:
        pass
```

Rerun `setup_host` afterward. Do not remove `.odibi-anchor-host-guidance.json` from a working
Anchor installation; it is the ownership and drift manifest used for safe reconciliation.

### 4. Launch and verify

```python
import runpy

namespace = runpy.run_path(
    f"{instruction_root}/.assistant/agent_bootstrap.py",
    init_globals={"ANCHOR_PROJECT_ID": project_id},
)
anchor = namespace["anchor"]
startup = namespace["STARTUP_PACKET"]

assert namespace["BOOTSTRAP"]["success"] is True
assert startup["status"] == "ready", startup
assert startup["project_id"] == project_id
```

Retain `namespace` and `anchor` for that Python process. After serverless compute replacement,
rerun the launcher. It prepares user-specific local state and restores the latest verified durable
snapshot. If the installed package is missing or stale, it returns the exact pinned install and
Python-restart remediation; apply that remediation and rerun the same launcher call.

## Create a launch-ready portfolio

The portfolio is user-owned routing metadata and contains no secrets. The following command
creates it once and refuses to overwrite an existing file:

```bash
anchor portfolio scaffold \
  --config /absolute/private/path/anchor.toml \
  --host local --adapter amp \
  --project example --target-root /absolute/path/to/example \
  --authority my-work \
  --local-state-root /absolute/local/odibi-anchor-state \
  --instruction-root /absolute/path/to/example
```

For Databricks, use the complete personal-workspace flow above. Its portfolio is persistent,
live state stays on local compute, and immutable snapshots use approved durable storage.

## Install host guidance

```bash
anchor setup-host amp --target /absolute/instruction/root
# adapters: amp, claude, chatgpt, databricks
```

The command installs `.assistant_instructions.md`, `.assistant/`, and the host pointer. It
tracks hashes, upgrades only unchanged managed files, and refuses collisions. It does not
silently create or change routing configuration.

## Prepare, diagnose, and launch

When a portfolio is configured, prepare it before setting environment variables or running
doctor. Do not point `ANCHOR_HOME` at the portfolio directory:

```bash
anchor portfolio validate --config /absolute/private/path/anchor.toml --host local
anchor portfolio prepare --config /absolute/private/path/anchor.toml --host local --project example
```

`portfolio prepare` returns the exact environment and bootstrap inputs. Apply every returned
environment field in the same persistent Python process, then run the returned launcher:

```python
import os
import runpy
from odibi_anchor import doctor, prepare_portfolio_runtime

prepared = prepare_portfolio_runtime(
    config_path="/absolute/private/path/anchor.toml",
    host_id="local",
    project_id="example",
)
os.environ.update(prepared["environment"])
diagnostics = doctor()
namespace = runpy.run_path(prepared["next_operation"]["arguments"]["script"])
anchor = namespace["anchor"]
status = anchor("status", output_format="dict")
```

The process-bound `anchor` callable comes from that namespace or from an explicit `launch()`
return value; it is not importable as `from odibi_anchor import anchor`. If doctor runs before
Databricks preparation, it remains read-only and directs the caller to `portfolio.prepare`
instead of asking the caller to invent `ANCHOR_HOME`.

Accept a task before loading its returned required skills. Load each with
`anchor("skill_loaded", "<name>")` in that same process; attempting to register a skill before
task acceptance is invalid. On Databricks, `launch()` automatically attaches read-only Git
Folder identity when the Workspace API can attest the configured target.

On Databricks, retain `ANCHOR_DURABLE_ROOT`; current releases qualify it through the Files
API rather than FUSE. After compute replacement, rerun preparation. It restores the absent
local database and managed project artifacts from the latest verified snapshot before launch.

## Add another project

```bash
anchor portfolio add-project \
  --config /absolute/private/path/anchor.toml \
  --host local --project another-project \
  --target-root /absolute/path/to/another-project
```

The target must already be a directory, but it does not have to be a Git repository. Git identity
is required only for source-change evidence. `add-project` protects its read/write with an
optimistic digest and returns an exact `portfolio.prepare` next operation; follow it to register,
restore, and bootstrap the new project. Supply `--expected-sha256` when a separately reviewed
portfolio digest must remain unchanged between approval and execution.

## Safe reset and cleanup

Do not delete by wildcard. Inventory each path and classify it first:

- active instruction/configuration;
- active local state;
- durable snapshots;
- release or qualification evidence;
- rollback/legacy backup;
- failed staging or disposable qualification state.

Back up and verify any active database before mutation. Remove only explicitly named
disposable paths after no process references them. A clean reinstall never requires deleting
legacy backups or production snapshots.
