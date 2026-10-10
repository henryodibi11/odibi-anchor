"""Real pytest subprocess polling through the managed dispatcher, without sleeps as evidence."""

from __future__ import annotations

import importlib
import threading
import time

import pytest

from tests._dispatcher.test_lifecycle_friction import (
    _git,
    _implemented,
)
from tests._dispatcher.test_lifecycle_friction import lifecycle as lifecycle


def _controlled_test(lifecycle, tmp_path, *, fail=False):
    release, counter = tmp_path / "release", tmp_path / "runs"
    (lifecycle.target / "test_poll.py").write_text(
        "from pathlib import Path\nimport time\n"
        "def test_controlled():\n"
        f"    with Path({str(counter)!r}).open('a') as stream: stream.write('run\\n')\n"
        "    deadline = time.monotonic() + 15\n"
        f"    while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        f"    assert Path({str(release)!r}).exists()\n"
        f"    assert {not fail!r}\n"
    )
    _git(lifecycle.target, "add", ".")
    _git(lifecycle.target, "commit", "-m", "controlled test")
    return release, counter


def _poll(anchor, operation):
    return anchor(operation["action"], *operation["args"], **operation["kwargs"])


def _finish(anchor, operation):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = _poll(anchor, operation)
        if result.get("status") != "running":
            return result
        time.sleep(0.02)
    pytest.fail("controlled pytest did not finish")


@pytest.mark.parametrize("fail", [False, True])
def test_polling_measures_once_only_on_completing_caller(lifecycle, tmp_path, monkeypatch, fail):
    release, counter = _controlled_test(lifecycle, tmp_path, fail=fail)
    _implemented(lifecycle, criteria_targets=("test_poll.py",))
    anchor = lifecycle.anchor
    evidence = importlib.import_module("odibi_anchor._dispatcher._workflow_evidence")
    collect = evidence.collect_test_measurement
    finalizer_threads = []

    def observed_finalize(**kwargs):
        finalizer_threads.append(threading.get_ident())
        return collect(**kwargs)

    monkeypatch.setattr(evidence, "collect_test_measurement", observed_finalize)
    try:
        first = anchor("test", target=["test_poll.py"], workflow_criterion="ok",
                       wait_seconds=0, request_id="controlled", output_format="dict")
        assert first["status"] == first["envelope"]["outcome"] == "running"
        assert "exit_code" not in first["metrics"]
        operation = first["next_operation"]
        assert operation["kwargs"]["request_id"] == "controlled"
        assert operation["copy_ready"] == first["envelope"]["next_operation"]["copy_ready"]
        assert _poll(anchor, operation)["status"] == "running"
        assert anchor("test", target=["test_poll.py"], workflow_criterion="ok",
                      wait_seconds=0, request_id="controlled", output_format="dict")["status"] == "running"
        assert not finalizer_threads
        assert anchor("workflow", output_format="dict")["state"]["measurements"] == {}
        timings = importlib.import_module("odibi_anchor._utils._session_state")._SESSION_TIMINGS
        assert all(row.get("executed") is False and not row["passed"]
                   for row in timings if row["action"] == "test")
    finally:
        release.touch()
    completed = _finish(anchor, operation)
    assert completed["metrics"]["passed"] == int(not fail)
    assert completed["metrics"]["failed"] == int(fail)
    assert completed["workflow_measurement"]["status"] == ("failed" if fail else "satisfied")
    assert finalizer_threads == [threading.get_ident()]
    assert counter.read_text() == "run\n"
    replay = _poll(anchor, operation)
    assert replay["request"]["replayed"] is True
    assert counter.read_text() == "run\n"
    assert finalizer_threads == [threading.get_ident()]


def test_poll_conflicts_and_lost_request_never_start_a_second_process(lifecycle, tmp_path):
    release, counter = _controlled_test(lifecycle, tmp_path)
    _implemented(lifecycle, criteria_targets=("test_poll.py",))
    anchor = lifecycle.anchor
    try:
        first = anchor("test", target=["test_poll.py"], wait_seconds=0, output_format="dict")
        operation = first["next_operation"]
        with pytest.raises(ValueError, match="different test arguments"):
            anchor("test", **{**operation["kwargs"], "target": ["test_pkg.py"]})
        original = (lifecycle.target / lifecycle.paths[0]).read_text()
        (lifecycle.target / lifecycle.paths[0]).write_text("VALUE = 99\n")
        with pytest.raises(ValueError, match="different file bytes"):
            _poll(anchor, operation)
        (lifecycle.target / lifecycle.paths[0]).write_text(original)
    finally:
        release.touch()
    _finish(anchor, operation)
    assert counter.read_text() == "run\n"
    with pytest.raises(ValueError, match="no such running request; rerun") as lost:
        anchor("test", **{**operation["kwargs"], "request_id": "lost-runtime-request"})
    assert lost.value.error_code == "test_running_request_missing"
    assert lost.value.context["executed"] is False
    assert lost.value.next_operation["kwargs"].get("poll") is not True
    assert counter.read_text() == "run\n"


def test_finite_wait_can_return_completed_result(lifecycle):
    anchor = lifecycle.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="poll", inline=True, output_format="dict")
    anchor("task", "Observe fixture tests", goal="Check fixture behavior", mode="analysis",
           acceptance_criteria=["fixture succeeds"], output_format="dict")
    result = anchor("test", target=["test_pkg.py"], wait_seconds=10, output_format="dict")
    assert result["metrics"]["passed"] == 1
    assert result["request"]["replayed"] is False
    assert result["envelope"]["outcome"] == "succeeded"


def test_dispatcher_restart_refuses_lost_inflight_request(lifecycle, tmp_path):
    release, counter = _controlled_test(lifecycle, tmp_path)
    _implemented(lifecycle, criteria_targets=("test_poll.py",))
    first = lifecycle.anchor("test", target=["test_poll.py"], wait_seconds=0, output_format="dict")
    # init evicts/reloads dispatcher modules, the same process-state loss as a restart.
    old_tools = importlib.import_module("odibi_anchor._dispatcher._session_tools")
    try:
        fresh = lifecycle.restart()
        assert fresh("task_rebind", output_format="dict")["status"] == "rebound"
        fresh("orient", output_format="dict")
        with pytest.raises(ValueError, match="no such running request; rerun"):
            _poll(fresh, first["next_operation"])
        assert not counter.exists() or counter.read_text() == "run\n"
    finally:
        release.touch()
        # Dispose children from the deliberately abandoned runtime, not a product recovery.
        for pending in old_tools._RETAINED_TEST_RESULTS.values():
            if "process" in pending:
                pending["process"].close()
                pending["steps"].close()


def test_subprocess_timeout_is_a_completed_failure(tmp_path):
    from odibi_anchor.pytest_runner import start_pytest

    (tmp_path / "test_timeout.py").write_text(
        "import time\ndef test_wait():\n    time.sleep(30)\n"
    )
    with start_pytest(["test_timeout.py", "-q"], cwd=tmp_path,
                      timeout=0.2, capture_output=True) as job:
        summary, process = job.wait(1)
        assert summary["timed_out"] is True
        assert process.returncode == summary["exit_code"] == -1
        assert summary["passed"] == 0
