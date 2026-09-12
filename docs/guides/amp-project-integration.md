# Integrate Odibi Anchor with an Amp project

Keep the host repository thin: install a pinned release into its Amp-managed virtual
environment, keep writable Odibi Anchor state outside the target checkout, expose only
the gateway tools, and tell Amp when to use them.

## 1. Install during setup

In `.agents/setup`, create/reuse `.venv` and install an exact released version (replace
`0.3.7` only as an intentional upgrade):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'odibi-anchor[mcp]==0.3.7'
```

When the release is not available from the configured package index, pin an immutable
public Git commit instead; never install a mutable branch:

```sh
.venv/bin/python -m pip install \
  'odibi-anchor[mcp] @ git+https://github.com/henryodibi11/odibi-anchor.git@<full-commit-sha>'
```

Setup and resume hooks should first validate the interpreter, import, and selected project;
return immediately when healthy. A resume hook should delegate to setup only when that check
fails, making fresh setup repeatable and resumed orbs self-healing.

## 2. Configure the gateway

Add `.amp/settings.json`:

```json
{
  "amp.mcpServers": {
    "odibi-anchor": {
      "command": "${AMP_WORKING_DIRECTORY}/.venv/bin/python",
      "args": ["-m", "odibi_anchor.mcp_server"],
      "env": {
        "ANCHOR_PROJECT_ID": "my-managed-project",
        "ANCHOR_PROJECT_ROOT": "${AMP_WORKING_DIRECTORY}",
        "ANCHOR_HOME": "/absolute/writable/state/odibi-anchor"
      },
      "includeTools": ["anchor_execute", "anchor_help"]
    }
  }
}
```

`ANCHOR_PROJECT_ID` must name an existing managed project in `ANCHOR_HOME`; startup verifies that
its registered target matches `ANCHOR_PROJECT_ROOT`. If the ID is omitted, startup succeeds only
when the target matches exactly one registry entry. It never creates or retargets a project.
`ANCHOR_HOME` must be an absolute, writable location outside that checkout (and outside the
installed package); do not commit its state. The public MCP server exposes only
`anchor_execute` and `anchor_help`, so writes cannot bypass the governed gateway.

The server freezes this route for its process lifetime. Changing cwd, environment variables,
or `workspace/.active_project` does not redirect it; deleting or retargeting its registry entry
blocks subsequent actions until the host restarts with valid configuration. The selector is an
interactive preference, not server authority. Temporary legacy deployments may opt in with
`ANCHOR_ALLOW_LEGACY_SELECTOR=1`, but should migrate to `ANCHOR_PROJECT_ID` and remove that flag.

For two concurrent Amp/MCP runtimes, give each process its own `ANCHOR_PROJECT_ID` and
`ANCHOR_PROJECT_ROOT`; the managed projects may share one `ANCHOR_HOME` only when their registered target
and artifact roots are distinct. After startup, run
`anchor("concurrency", command="inspect")` through `anchor_execute` and verify `binding_source`,
`project_id`, `target_root`, `artifact_root`, `runtime_instance_id`, and the route fingerprint for
each server. Never use `.active_project` to coordinate servers. Same-project conflicting writers
are not supported; use one writer or separate source checkouts and integrate through Git.

Before enabling the claim, follow the [concurrency release gate and crash
runbook](runtime-rollout.md#concurrent-project-operations). If diagnostics block, preserve their
bounded output and use separate per-project `ANCHOR_HOME` values as containment rather than weakening
route or owner checks.

## 3. Add host instructions

Add a short section to the target repository's `AGENTS.md` telling Amp to use the configured
Odibi Anchor MCP gateway proactively for ordinary substantive prompts, proportionately
to risk; register changed paths with `touched`; and run applicable lifecycle checks before
delivery. Keep detailed Odibi Anchor operating policy in its canonical host instructions
instead of copying it into `AGENTS.md`.
