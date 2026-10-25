# Agent-first workflows (0.4.0)

Anchor is the framework agents use end to end: planning, implementation and delivery. A host
harness (OpenCode, Databricks, Amp) only maps its roles onto Anchor and supplies what Anchor
cannot observe. Every process rule below lives in Anchor, so it behaves the same on every host.

## Owner touchpoints

The owner is needed at four points only:

1. Approve a plan (spec plus its work items).
2. Approve a delivery (push, pull request, release).
3. Decide pending lessons (open observations and candidate memories).
4. Resolve a real blocker that Anchor refuses and the agent cannot fix.

## Owner approval

One rule for plan approval, delivery, revocation and memory promotion.

- The challenge is `APPROVE <64 lowercase hex>` (or `REVOKE <64 hex>`), the SHA-256 of the exact
  subject. It never carries a `sha256:` prefix, so every transport can match it.
- Channels open at the same time and the first valid reply wins:
  - Slack (`ANCHOR_SLACK_*`): authenticated remote identity, works from a phone.
  - In-session chat (`provider="chat_in_session"`): the host confirms the owner typed the
    reply in the session. Recorded as lower assurance, like `databricks_in_session`.
  - Databricks in-session, and the local Windows dialog when no other channel is configured.
- A request never blocks for long. `request_*_approval` posts the challenge, waits at most
  `wait_seconds`, and otherwise returns `status: "pending"` with two copy-ready operations:
  check again, or complete with the in-session reply. The pending request expires after
  `timeout_minutes` (default 30); the agent then stops and reports that approval is waiting.
- A challenge is single use. The first accepted reply consumes it; Anchor closes the other
  channel (Slack gets a thread reply saying the request was answered elsewhere) and rejects
  late replies.

## Modes are contracts

Each task mode publishes a contract: the fields to fill, the evidence that counts, the review
its rigor requires, and where the owner approves. `anchor("help", "mode", "<mode>")` returns it,
and an accepted task carries `mode_contract`. Agents fill everything the contract asks for and
follow `next_operation`.

Rigor scales the ceremony:

| Rigor | Plan reviews before owner approval | Delivery review |
|---|---|---|
| direct | 1 approving review | self review allowed (unless high risk) |
| compact | 2 approving reviews, distinct angles | self review allowed (unless high risk) |
| full | 4 approving reviews, distinct angles, one `adversarial` | independent review required |

Documentation in a repository is a `documentation` task with `execution_mode="source_change"`
and command or review criteria. Pipeline and configuration work (YAML, pipeline config) uses
the `pipeline` mode.

## Criteria

A workflow criterion names how it is measured:

| method | Measured by | Evidence |
|---|---|---|
| `pytest` | `anchor("test", target=[...], workflow_criterion=id)` | counts, environment |
| `command` | `anchor("test", workflow_criterion=id)` runs the plan's `argv` | exit code, output digest and tail, environment |
| `review` | the workflow review | the review verdict and findings |
| `artifact_sha256` | `workflow check_artifact` | expected and observed digests |

A command criterion is `{"id", "method": "command", "expected", "argv": [...],
"environment"?, "cwd"?, "timeout_seconds"?, "expected_exit_code"?}`. `argv` runs without a
shell, in the target root (or `cwd` under it). No test file is needed for work that has no tests.

## Test environments

`projects.<id>.test_environments.<host>.<name>` in the portfolio names where criteria run:

```toml
[projects."odibi-agent".test_environments.win-desktop.windows]
kind = "local"
python = "C:\\Users\\me\\.venvs\\odibi-dev\\Scripts\\python.exe"

[projects."odibi-agent".test_environments.win-desktop.wsl-spark]
kind = "wsl"
distro = "Ubuntu"
python = "/home/me/venvs/odibi-spark/bin/python"
```

`kind = "runtime"` uses Anchor's own interpreter (for example on Databricks). A criterion's
`environment` selects one; without it, the project's `test_python` or Anchor's interpreter
runs as before. Evidence records the environment name and kind.

## Planning records

The planner writes Anchor records directly instead of a separate plans folder:

- Problem Record: the investigation (`problem create/update`).
- Spec: the shared design (`spec create` with `content`, `spec update`).
- Work items: one pull request each, with `depends_on` (work item IDs).
- Reviews: `work_item review` records the angle, verdict, findings and reviewer, bound to the
  work item's fingerprint. A later edit makes the review stale.
- `work_item next` returns the items whose dependencies are completed.
- `work_item request_plan_approval` asks the owner to approve the spec and its work items
  once the reviews satisfy the rigor; the approval binds their exact fingerprints.

## Lessons

`gate` and `learning assess` results carry `pending_owner_decisions`: open observations and
candidate memories, each with its options (activate, reject, defer, derive) and the copy-ready
operation for each. The agent shows them in its report; the owner decides; the agent records
the decision through Anchor.
