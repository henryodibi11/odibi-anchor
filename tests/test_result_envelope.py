"""Result envelope contract and dispatch-table ratchet.

The ratchet boots a real dispatcher and calls every built-in action. Each call must
return a dict with an ``envelope`` (or raise an exception carrying ``.envelope``) whose
``outcome`` is one of the four allowed values. Every mutating invocation must report
``effects`` and ``retry_safety``. ``ENVELOPE_EXEMPT`` lists actions that return a
non-dict value and therefore cannot carry the additive key; it may only shrink.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher import _envelope
from odibi_anchor._recovery import attach_recovery, dispatcher_operation

OUTCOMES = {"succeeded", "succeeded_with_warnings", "blocked", "failed"}

# Actions whose result is not a dict. Shrink only: an exempt action that starts
# returning a dict fails the ratchet until it is removed from this mapping.
ENVELOPE_EXEMPT = {
    "export_md": "returns markdown text",
    "help": "returns help text",
    "session_log": "returns a list of log entries",
}


def _target(tmp_path):
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
    return target


@pytest.fixture
def launched(tmp_path, monkeypatch, capsys):
    from odibi_anchor.startup import launch, register_project

    for name in ("ANCHOR_HOME", "ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    target = _target(tmp_path)
    register_project(anchor_home=home, project_id="alpha", project_root=target)
    anchor = launch(anchor_home=home, project_id="alpha", project_root=target)
    boot_output = capsys.readouterr()
    return SimpleNamespace(anchor=anchor, home=home, target=target, boot_output=boot_output)


def _envelope_of(anchor, action, *args, **kwargs):
    try:
        result = anchor(action, *args, **kwargs)
    except Exception as exc:  # the ratchet inspects every failure's envelope
        return "raised", getattr(exc, "envelope", None)
    if isinstance(result, dict):
        return "dict", result.get(_envelope.ENVELOPE_KEY)
    return type(result).__name__, None


# ── Unit contract ────────────────────────────────────────────────────────────


def test_failure_envelope_maps_attach_recovery_fields():
    exc = attach_recovery(
        ValueError("descriptor changed"),
        error_code="project_descriptor_changed",
        context={"project_id": "alpha"},
        next_operations=[dispatcher_operation(
            "project", "status", reason="refresh", requires_owner=True, retry_safety="state_checked",
        )],
    )

    envelope = _envelope.build_failure_envelope("project", exc, args=("use",), effects=("artifact_write",))

    assert envelope["outcome"] == "blocked"  # owner decision required
    assert envelope["error"] == {
        "type": "ValueError", "error_code": "project_descriptor_changed", "message": "descriptor changed",
    }
    assert envelope["next_operation"] == {
        "copy_ready": "anchor('project', 'status')", "requires_owner": True,
        "retry_safety": "state_checked", "reason": "refresh",
    }
    assert envelope["effects"]["changed"] is None
    assert envelope["effects"]["verified_readback"] is False
    assert envelope["retry_safety"] == "not_idempotent"


def test_legacy_blocked_message_yields_the_recovery_call_not_the_refused_call():
    exc = RuntimeError(
        'BLOCKED: anchor("touched") on .py files requires known-bad check first. '
        'Run anchor("known_bad", changed_files=["a.py"])\nCheck first.'
    )

    envelope = _envelope.build_failure_envelope("touched", exc, effects=("artifact_write",))

    assert envelope["outcome"] == "blocked"
    assert envelope["next_operation"]["copy_ready"] == 'anchor("known_bad", changed_files=["a.py"])'
    assert envelope["next_operation"]["retry_safety"] == "read_only"


def test_dict_outcomes_are_exclusive_and_warnings_are_explicit():
    blocked = _envelope.build_envelope("gate", {"passed": False, "status": "blocked"})
    failed = _envelope.build_envelope("gate", {"passed": False})
    gap = _envelope.build_envelope(
        "task", {"readiness": {"status": "needs_clarification", "score": 55,
                               "missing_details": ["Constraints are missing."]}},
    )
    degraded = _envelope.build_envelope(
        "status", {"kind": "session_status"},
        degraded=[{"component": "boot_frame_init", "reason": "unavailable"}],
    )

    assert (blocked["outcome"], failed["outcome"]) == ("blocked", "failed")
    assert gap["outcome"] == "succeeded_with_warnings"
    assert gap["warnings"][0]["code"] == "readiness_gap"
    assert degraded["outcome"] == "succeeded_with_warnings"
    assert degraded["warnings"] == [{
        "code": "degraded", "component": "boot_frame_init", "message": "unavailable",
        "fallback_used": True,
    }]


def test_mutating_success_without_a_readback_probe_is_not_verified():
    envelope = _envelope.build_envelope("save", {"kind": "save"}, effects=("artifact_write",))

    assert envelope["effects"]["changed"] is True
    assert envelope["effects"]["verified_readback"] is False
    assert envelope["undo"]["status"] == "irreversible"


def test_compact_task_projection_meets_budget_and_keeps_decision_fields():
    selections = [
        {"selection_id": f"sel_{index}", "memory_id": f"mem_{index}", "summary": "s" * 400,
         "provenance": {"source": "x"}, "content": "c" * 4000}
        for index in range(5)
    ]
    full = {
        "kind": "task_execution_context", "status": "ready", "task_window_id": "ltw_1",
        "readiness": {"status": "ready", "score": 90, "missing_details": ["m"] * 9},
        "memory_context": {"selection_count": 5, "selections": selections},
        "plan": ["p" * 2000] * 20, "handoff": {"prompt_brief": "h" * 20000},
        "agent_context": {"next_operation": {"action": "preflight", "copy_ready": 'anchor("preflight")'}},
        "envelope": {"outcome": "succeeded"},
    }

    compact = _envelope.compact_task_result(full)

    assert _envelope._estimate_tokens(compact) <= _envelope.COMPACT_TASK_TOKEN_BUDGET
    assert compact["envelope"] == full["envelope"]
    assert compact["task_window_id"] == "ltw_1"
    assert [item["selection_id"] for item in compact["memory_context"]["selections"]] == [
        f"sel_{index}" for index in range(5)
    ]
    assert {"plan", "handoff"} <= set(compact["transport"]["omitted_sections"])


def test_read_only_readiness_is_ready_once_the_task_is_acceptable():
    from odibi_anchor.planning._task_builders import _build_readiness

    readiness = _build_readiness(
        goal="Explain the module.", desired_outcome=None, background=None, current_state=None,
        trigger=None, constraints=[], in_scope=[], out_of_scope=[], artifacts=[], inputs=[],
        dependencies=[], acceptance_criteria=["Explain."], risks=[], stop_conditions=[],
        deliverables=[], expected_output_format=None, requester=None, executor=None,
        evidence=[], evidence_gaps=[], known_facts=None, mode="analysis",
    )

    assert readiness["score"] == 50  # accepted: post-dispatch minimum is 40
    assert readiness["status"] == "ready"
    assert readiness["missing_details"]  # gaps remain advisory


# ── Real dispatcher ──────────────────────────────────────────────────────────


def test_bootstrap_banner_goes_to_stderr_not_stdout(launched):
    assert "[Anchor]" not in launched.boot_output.out
    assert "[Anchor] Ready" in launched.boot_output.err


def test_new_session_reports_what_it_did(launched):
    anchor = launched.anchor
    anchor("orient")
    before = anchor("status")["runtime"]["session_id"]

    result = anchor("new_session", name="envelope_session")

    after = anchor("status")["runtime"]["session_id"]
    assert result["status"] == "configured"
    assert result["session_id"] == after != before
    assert result["reset"]["files_changed_cleared"] is True
    effects = result["envelope"]["effects"]
    assert effects["verified_readback"] is True
    assert effects["readback"]["method"] == "runtime_session_state"
    assert "session_file_ledger" in effects["invalidated"]


def test_every_dispatch_table_action_returns_an_envelope(launched):
    from odibi_anchor._dispatcher._effects import (
        BUILTIN_ACTION_NAMES,
        build_static_action_contracts,
        resolve_invocation,
    )

    anchor = launched.anchor
    contracts = build_static_action_contracts(anchor_home=launched.home)
    anchor("orient")
    anchor("new_session", name="ratchet")
    accepted = anchor(
        "task", "Inspect the module.", goal="Explain module.py.", mode="analysis",
        acceptance_criteria=["Explain."],
    )
    assert accepted["envelope"]["outcome"] == "succeeded"
    assert accepted["envelope"]["effects"]["verified_readback"] is True

    covered, non_dict, problems = set(), set(), []
    for action in sorted(BUILTIN_ACTION_NAMES - {"task", "new_session"}):
        shape, envelope = _envelope_of(anchor, action)
        if shape not in {"dict", "raised"}:
            non_dict.add(action)
            continue
        if not isinstance(envelope, dict) or envelope.get("outcome") not in OUTCOMES:
            problems.append(f"{action}: missing or invalid envelope ({shape})")
            continue
        resolution = resolve_invocation(contracts[action], (), {})
        if _envelope.is_mutating(resolution.effects) and (
            not isinstance(envelope.get("effects"), dict) or not envelope.get("retry_safety")
        ):
            problems.append(f"{action}: mutating invocation without effects/retry_safety")
        if envelope["schema"] != _envelope.ENVELOPE_SCHEMA or envelope["version"] != 1:
            problems.append(f"{action}: wrong schema")
        covered.add(action)

    assert problems == []
    assert non_dict == set(ENVELOPE_EXEMPT), "update ENVELOPE_EXEMPT (it may only shrink)"
    total = len(BUILTIN_ACTION_NAMES)
    coverage = (len(covered) + 2) / total  # + task and new_session asserted above
    assert coverage == (total - len(ENVELOPE_EXEMPT)) / total


def test_in_process_compact_task_is_budgeted(launched):
    anchor = launched.anchor
    anchor("orient")
    anchor("new_session", name="compact")
    compact = anchor(
        "task", "Inspect the module.", goal="Explain module.py.", mode="analysis",
        acceptance_criteria=["Explain."], response_detail="compact",
    )

    assert _envelope._estimate_tokens(compact) <= _envelope.COMPACT_TASK_TOKEN_BUDGET
    assert compact["transport"]["response_detail"] == "compact"
    assert compact["envelope"]["outcome"] == "succeeded"
    assert compact["readiness"]["status"] == "ready"
    assert len(json.dumps(compact)) < 8000

    with pytest.raises(ValueError, match="response_detail"):
        anchor("status", response_detail="verbose")
