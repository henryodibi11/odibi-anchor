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

For Databricks, live SQLite must stay on local compute while snapshots are durable:

```python
from odibi_anchor.portfolio import scaffold_portfolio

portfolio = scaffold_portfolio(
    "/tmp/anchor.toml",
    host_id="databricks-work",
    adapter="databricks",
    project_id="example",
    target_root="/Workspace/Users/name/example",
    authority_id="my-work",
    local_state_root="/tmp/odibi-anchor",
    instruction_root="/Workspace/Users/name",
    durable_root="/Volumes/catalog/schema/odibi-anchor",
)
assert portfolio["validation"]["status"] == "valid"
```

Use a persistent private location for the real portfolio. `/tmp/anchor.toml` above is a
notebook example, not durable configuration.

## Install host guidance

```bash
anchor setup-host amp --target /absolute/instruction/root
# adapters: amp, claude, chatgpt, databricks
```

The command installs `.assistant_instructions.md`, `.assistant/`, and the host pointer. It
tracks hashes, upgrades only unchanged managed files, and refuses collisions. It does not
silently create or change routing configuration.

## Diagnose, prepare, and launch

Run doctor before guessing:

```bash
anchor doctor
anchor portfolio validate --config /absolute/private/path/anchor.toml --host local
anchor portfolio prepare --config /absolute/private/path/anchor.toml --host local --project example
```

`portfolio prepare` returns the exact environment and bootstrap inputs. Apply every returned
environment field in the same persistent Python process, then launch:

```python
import os
from odibi_anchor import launch, prepare_portfolio_runtime

prepared = prepare_portfolio_runtime(
    config_path="/absolute/private/path/anchor.toml",
    host_id="local",
    project_id="example",
)
os.environ.update(prepared["environment"])
anchor = launch(**prepared["bootstrap"]["arguments"])
status = anchor("status", output_format="dict")
```

On Databricks, retain `ANCHOR_DURABLE_ROOT`; current releases qualify it through the Files
API rather than FUSE. After compute replacement, rerun preparation. It restores an absent
local database from the latest verified snapshot before launch.

## Add another project

```bash
anchor portfolio add-project \
  --config /absolute/private/path/anchor.toml \
  --host local --project another-project \
  --target-root /absolute/path/to/another-project \
  --expected-sha256 <digest-returned-by-portfolio-show>
```

The digest protects against concurrent edits. Every runtime must still prepare and bind one
exact project and root.

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
