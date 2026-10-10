# Incident replay harness

End-to-end replays of real Odibi Anchor incidents that cross module boundaries:
portfolio, `PROJECT.md`, continuity `OWNER.json`, durable snapshots and a new
compute identity. Each scenario drives public entry points
(`bootstrap_managed_project`, the returned `anchor` callable, durability and
portfolio functions, the MCP gateway) in one pytest process, against a fake
Databricks runtime. Hardening fixes get red-to-green proof here, and releases can
qualify against the same failure modes from an installed wheel.

## Pieces

- `tests/fixtures/fake_databricks/`
  - `FakeDatabricks` installs fake `databricks.sdk`, `databricks.sdk.errors` and
    `databricks.sdk.service.workspace` modules in `sys.modules`, sets
    `DATABRICKS_RUNTIME_VERSION`, and patches `os.geteuid`/`pwd.getpwuid`. These are
    the only seams. Product modules are never patched, because `bootstrap.init`
    evicts and re-imports every `odibi_anchor` module.
  - `WorkspaceClient().workspace` is an in-memory store (`get_status`, `download`,
    `upload`, `mkdirs`, `delete`, `list`). `WorkspaceClient().files` is backed by a
    temporary directory per provisioned Volume (`list_directory_contents`,
    `download_to`, `upload_from`, `create_directory`, `get_directory_metadata`,
    `delete`). Only methods Anchor calls, or that the v0.3.24 hardening needs
    (`get_directory_metadata`, `workspace.list`), are modeled. Paths outside a
    provisioned Volume raise `NotFound`, as on Databricks.
  - `set_latency(seconds, api=..., method=...)` delays calls.
    `inject_fault(api, method, error=..., match=..., times=..., when="before"|"after")`
    fails them. `when="after"` applies a mutation and then raises, which models a
    lost response. `calls` logs every call.
  - `new_process()` models a Python restart: it drops `ANCHOR_*` bindings.
    `new_compute()` also wipes the local disk (`local_root`, the `/tmp` stand-in)
    and changes the OS account. That changes the compute-identity fingerprint, so
    startup selects a new identity-isolated runtime root. The UID stays the real
    one because startup compares it with the owner of directories the test
    creates.
  - `write_databricks_portfolio(...)` writes a disposable PortfolioV1 with a
    `databricks` host through the public portfolio API.
  - `crash.CrashInjector(scope=[...], crash_at=N | crash_if=pred, error=..., hook=...)`
    numbers every in-scope filesystem mutation and every fake remote mutation. The
    filesystem primitives are `os.replace/rename/link/symlink/mkdir`, write-mode
    `os.open`/`open`/`io.open`/`tarfile.bltn_open`, and `shutil.copytree/move`. It
    raises before the chosen point. The default `InjectedCrash` is a
    `BaseException`: `except Exception` recovery does not run, while `finally`
    blocks do. `crash_matrix(prepare=, operation=, verify=, scope=)` crashes before
    every point in turn and verifies invariants after each crash. SQLite page
    writes and deletions are not mutation points.
- `tests/integration/conftest.py`
  - The `replay` fixture provides a `ReplayHarness`: persistent instruction root,
    portfolio and targets (the Workspace FUSE stand-ins), plus `create_project`,
    `bootstrap`, `start_analysis_task`, `snapshot`, `new_process` and
    `new_compute`.
  - The `workflow` fixture admits a disposable plan through the real
    inquiry → `workflow create` → producer → `accept_plan` sequence over any
    transport.
  - Every test starts with no `ANCHOR_*` variables and gets its environment
    restored afterward.
- `test_lifecycle_friction_replay.py` drives one high-risk workflow candidate with more than 15
  planned files from inquiry through a dispatcher restart to `qualify` on a local Git target
  (issue #39). It needs no fake Databricks runtime.
- `test_incident_replay.py` holds the scenarios. `test_replay_harness.py` holds
  the harness's own contract tests, including a crash matrix over durable snapshot
  publication.

## Adding an incident

1. Reproduce it with public calls only: `replay.create_project`, an agent action
   through `created["anchor"]`, `replay.new_compute()`/`new_process()`, then
   `replay.bootstrap`. Use `CrashInjector(hook=...)` for interleavings, such as a
   competing writer, and `crash_if=`/`error=` for targeted faults.
2. Assert the shared error-code contract: `exc.error_code` and its key `exc.context`
   fields, plus durable facts such as preserved bytes. Never assert message text.
3. Confirm the test fails on unchanged code for the incident's reason. If the fix
   is still in flight, mark it
   `@pytest.mark.xfail(strict=True, reason="#<issue> <workstream> pending: <code>")`
   and remove the marker when the fix merges. Strict mode turns an unexpected pass
   into a failure, so the flip cannot be missed.
4. If a behavior cannot be reached without patching product code, report the
   missing seam instead of patching it.

## Running

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration -q
./run_tests.sh tests/integration           # canonical runner; also collected by `tests/`
```

To qualify an installed wheel, build it, install it into a separate virtual
environment with the test dependencies, and keep `src/` and the parent conftest
off the import path:

```sh
python -m build --wheel -o /tmp/anchor-dist      # or: uv build --wheel -o /tmp/anchor-dist
python -m venv /tmp/anchor-wheel
/tmp/anchor-wheel/bin/pip install "/tmp/anchor-dist/odibi_anchor-<version>-py3-none-any.whl[mcp]" \
    pandas pytest pytest-timeout
INCIDENT_REPLAY_EXPECT_INSTALLED=1 PYTHONDONTWRITEBYTECODE=1 /tmp/anchor-wheel/bin/python -m pytest \
    -o pythonpath=. --confcutdir=tests/integration -p no:cacheprovider -q tests/integration
```

With `INCIDENT_REPLAY_EXPECT_INSTALLED=1` the run aborts if `odibi_anchor` would be
imported from this checkout.

## Known seam gap

`bootstrap_managed_project` requires `instruction_root` to be an existing local
directory, and `setup_host` uses the Workspace API only for `/Workspace/...` roots.
A `/Workspace` instruction root therefore cannot be bootstrapped off Databricks.
The scenarios publish host guidance through the fake Workspace with
`setup_host("/Workspace/...", adapter="databricks")` and bootstrap from a local
instruction root.
