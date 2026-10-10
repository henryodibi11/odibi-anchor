"""End-to-end result envelope journey over one real dispatcher and every transport.

A disposable managed project is registered and launched through the public startup
API. The same bound dispatcher then serves in-process calls, the CLI (`anchor exec`)
and the MCP gateway (`anchor_execute`, response_version 2), so the scenario proves
that an agent sees the same outcome, state, effects and next operation regardless of
transport, for successes and for a policy block.
"""
from __future__ import annotations

import json
import subprocess

import pytest

import odibi_anchor.mcp_server as mcp_server
from odibi_anchor import cli
from odibi_anchor._dispatcher._envelope import COMPACT_TASK_TOKEN_BUDGET
from odibi_anchor._utils._output_hints import estimate_tokens


@pytest.fixture
def journey(tmp_path, monkeypatch, capsys):
    from odibi_anchor.startup import launch, register_project

    for name in ("ANCHOR_HOME", "ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (target / "module.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(target), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    home = tmp_path / "home"
    register_project(anchor_home=home, project_id="alpha", project_root=target)
    anchor = launch(anchor_home=home, project_id="alpha", project_root=target)
    boot = capsys.readouterr()
    monkeypatch.setattr(cli, "_boot", lambda _root: anchor)
    monkeypatch.setattr(mcp_server, "_boot", lambda: anchor)
    monkeypatch.setattr(mcp_server, "_table_cache", {})
    return anchor, boot


def _cli(capsys, action, kwargs=None, args=None):
    code = cli.main(["exec", action, json.dumps({"args": args or [], "kwargs": kwargs or {}})])
    return code, json.loads(capsys.readouterr().out)


def _mcp(action, kwargs=None, args=None, **options):
    request = json.dumps({"args": args or [], "kwargs": kwargs or {}})
    return mcp_server.anchor_execute(action, request, response_version=2, **options)


def test_agent_journey_sees_one_envelope_on_every_transport(journey, capsys):
    anchor, boot = journey
    assert boot.out == ""  # the banner never reaches an agent's stdout
    assert "[Anchor]" in boot.err

    orient = anchor("orient")["envelope"]
    assert orient["outcome"] == "succeeded"
    assert orient["state"]["project"] == "alpha"
    assert orient["next_operation"]["copy_ready"].startswith('anchor("new_session"')

    session = anchor("new_session", name="journey")
    assert session["status"] == "configured" and session["session_id"]
    assert session["envelope"]["effects"]["verified_readback"] is True

    # MCP default: a compact, budgeted task that still says whether it worked.
    task_text = _mcp("task", {
        "goal": "Explain module.py.", "mode": "analysis", "acceptance_criteria": ["Explain."],
    }, args=["Inspect the module."])
    task_response = json.loads(task_text)
    assert task_response["ok"] is True, task_response.get("error")
    task = task_response["result"]
    assert estimate_tokens(json.dumps(task, separators=(",", ":"))) <= COMPACT_TASK_TOKEN_BUDGET
    assert task["status"] == task["readiness"]["status"] == "ready"
    assert task["envelope"]["outcome"] == "succeeded"
    assert task["envelope"]["state"]["task_window_id"] == task["task_window_id"]
    assert task["envelope"]["effects"]["verified_readback"] is True

    # A read-only call: identical envelope in-process, over the CLI and over MCP.
    in_process = anchor("status")["envelope"]
    code, cli_status = _cli(capsys, "status")
    mcp_status = json.loads(_mcp("status"))
    assert code == 0 and mcp_status["ok"] is True
    assert in_process["outcome"] == "succeeded" and in_process["effects"] is None
    assert cli_status["result"]["envelope"] == in_process == mcp_status["result"]["envelope"]

    # A policy block: the same blocked envelope travels on the exception and both errors.
    with pytest.raises(RuntimeError) as raised:
        anchor("touched", "module.py")
    blocked = raised.value.envelope
    code, cli_blocked = _cli(capsys, "touched", args=["module.py"])
    mcp_blocked = json.loads(_mcp("touched", args=["module.py"]))
    assert blocked["outcome"] == "blocked"
    assert blocked["effects"] == {
        "changed": None, "verified_readback": False, "readback": None, "invalidated": [],
        "effect_classes": ["artifact_write"],
    }
    assert code == cli.EXIT_POLICY and mcp_blocked["ok"] is False
    assert cli_blocked["error"]["envelope"] == blocked == mcp_blocked["error"]["envelope"]

    # Closing the read-only task reports a governance effect, never a silent pass.
    closed = anchor("learning", "assess", outcome="nothing_reusable_learned")["envelope"]
    assert closed["outcome"] in {"succeeded", "succeeded_with_warnings"}
    assert closed["effects"]["changed"] is True
    assert closed["retry_safety"] == "not_idempotent"
