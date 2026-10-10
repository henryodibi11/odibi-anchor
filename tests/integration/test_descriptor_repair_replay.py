"""Replay #29 end to end: a damaged PROJECT.md is repaired through the portfolio and boots.

Drives public entry points only (managed bootstrap, the returned ``anchor`` callable,
the portfolio repair API and durable snapshots) on the fake Databricks runtime, and
asserts the error-code contract, never message text.
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any

import pytest

PLAIN_MARKDOWN_DESCRIPTOR = "# Alpha\n\nTeam notes about this project.\n"


def _recovery(exc: BaseException) -> tuple[str | None, dict[str, Any], list[dict[str, Any]]]:
    return (
        getattr(exc, "error_code", None),
        dict(getattr(exc, "context", {}) or {}),
        list(getattr(exc, "next_operations", []) or []),
    )


def test_damaged_descriptor_new_compute_repair_then_boot(replay):
    target = replay.target("alpha")
    created = replay.create_project("alpha", target)
    descriptor = Path(created["startup_packet"]["artifact_root"]) / "PROJECT.md"
    descriptor.write_text(PLAIN_MARKDOWN_DESCRIPTOR, encoding="utf-8")
    replay.snapshot(created)
    replay.new_compute()

    with pytest.raises(Exception) as caught:
        replay.bootstrap("alpha")

    code, context, operations = _recovery(caught.value)
    assert code == "managed_descriptor_damaged"
    assert context["supported_repair_available"] is True
    assert Path(context["config_path"]) == replay.config
    repair = operations[0]
    assert repair["operation"] == "repair_descriptor" and repair["requires_owner"] is True
    restored_descriptor = Path(context["descriptor_path"])
    damaged_sha = hashlib.sha256(PLAIN_MARKDOWN_DESCRIPTOR.encode()).hexdigest()
    assert repair["arguments"]["expected_sha256"] == damaged_sha
    startup = importlib.import_module("odibi_anchor.startup")

    plan = startup.repair_portfolio_descriptor(**repair["arguments"])
    assert plan["status"] == "plan"
    assert restored_descriptor.read_text(encoding="utf-8") == PLAIN_MARKDOWN_DESCRIPTOR
    approved = startup.repair_portfolio_descriptor(**plan["next_operations"][0]["arguments"])

    assert approved["status"] == "repaired"
    assert approved["route_fields"]["target_root"] == str(target)
    assert restored_descriptor.read_text(encoding="utf-8").endswith(PLAIN_MARKDOWN_DESCRIPTOR)
    assert Path(approved["backup_path"]).read_text(encoding="utf-8") == PLAIN_MARKDOWN_DESCRIPTOR
    booted = replay.bootstrap("alpha")
    packet = booted["startup_packet"]
    assert (packet["project_id"], packet["target_root"]) == ("alpha", str(target))
    assert packet["artifact_root"] == str(restored_descriptor.parent)
    # The repaired route accepts fresh task authority and checkpoints again.
    anchor = booted["anchor"]
    replay.start_analysis_task(anchor)
    closure = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    assert closure["accepted_task_closure"]["status"] == "closed"


@pytest.mark.parametrize("edit", ["body", "project_type"])
def test_gate_protects_descriptor_route_fields_but_allows_body_edits(replay, workflow, edit):
    target = replay.target("alpha")
    created = replay.create_project("alpha", target)
    anchor = created["anchor"]

    def call(action, *args, **kwargs):
        return anchor(action, *args, output_format="dict", **kwargs)

    workflow.accept(call, workflow.plan("artifact_only", ["PROJECT.md"]), name="descriptor_edit")
    descriptor = Path(created["startup_packet"]["artifact_root"]) / "PROJECT.md"
    text = descriptor.read_text(encoding="utf-8")
    if edit == "body":
        descriptor.write_text(text + "\n## Owner notes\n\nA body-only edit.\n", encoding="utf-8")
    else:
        descriptor.write_text(text.replace("project_type: referenced", "project_type: managed"),
                              encoding="utf-8")
    # Any PROJECT.md write makes the bound dispatcher stale; continue the accepted task
    # from a fresh process, as an agent must.
    replay.new_process()
    anchor = replay.bootstrap("alpha")["anchor"]
    call("task_rebind")
    call("touched", str(descriptor))
    call("review")

    if edit == "body":
        gate = call("gate")
        assert gate["obligations"] == [] or gate["obligations_paid"]
        closed = call("learning", "assess", outcome="nothing_reusable_learned")
        assert closed["accepted_task_closure"]["status"] == "closed"
        return
    with pytest.raises(Exception) as caught:
        call("gate")
    code, context, _operations = _recovery(caught.value)
    assert code == "managed_descriptor_route_change"
    assert context["changed_fields"] == ["project_type"]
