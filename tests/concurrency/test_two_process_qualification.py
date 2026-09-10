"""Deterministic production-stdio qualification for distinct project runtimes."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

_TIMEOUT = 30
_WORKER = textwrap.dedent(
    r"""
    import asyncio
    import json
    import os
    import sys

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    def emit(value):
        sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
        sys.stdout.flush()

    async def main():
        transport = StdioTransport(
            command=sys.executable,
            args=["-B", "-m", "odibi_anchor.mcp_server"],
            env=os.environ.copy(),
            cwd=os.environ["ANCHOR_PROJECT_ROOT"],
        )
        async with Client(transport, timeout=30) as client:
            tools = sorted(tool.name for tool in await client.list_tools())
            emit({"kind": "READY", "pid": os.getpid(), "tools": tools})
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    return
                request = json.loads(line)
                if request["kind"] == "STOP":
                    emit({"kind": "STOPPED", "id": request["id"]})
                    return
                try:
                    arguments = {
                        "action": request["action"],
                        "response_version": 2,
                        "response_detail": "full",
                    }
                    if request.get("args"):
                        arguments["args"] = json.dumps(request["args"])
                    response = await client.call_tool("anchor_execute", arguments, timeout=30)
                    envelope = json.loads(response.content[0].text)
                    emit({"kind": "RESULT", "id": request["id"], "envelope": envelope})
                except Exception as exc:
                    emit({
                        "kind": "CLIENT_ERROR",
                        "id": request["id"],
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    })

    asyncio.run(main())
    """
)

_TASK = {
    "mode": "implementation",
    "execution_mode": "artifact_only",
    "current_state": "An isolated concurrency qualification target exists.",
    "desired_outcome": "The bound MCP lifecycle remains owner-isolated.",
    "constraints": ["Do not modify target source."],
    "known_facts": ["The runtime has an immutable managed-project binding."],
    "evidence": [{"source": "qualification", "observation": "isolated fixture"}],
    "in_scope": ["MCP process isolation"],
    "out_of_scope": ["same-project source writes"],
    "risks": ["cross-owner state"],
    "acceptance_criteria": ["Gate and owner-scoped learning remain isolated."],
    "stop_conditions": ["owner identity mismatch"],
    "deliverables": ["qualification evidence"],
}


class RemoteCallError(RuntimeError):
    """One MCP action returned its normal version-2 error envelope."""


class StdioWorker:
    """Parent-controlled worker that owns one production FastMCP stdio server."""

    def __init__(self, *, project: str, target: Path, home: Path, runtime: str):
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANCHOR_HOME": str(home),
            "ANCHOR_MEMORY_DB": str(home / ".agent_memory.db"),
            "ANCHOR_PROJECT_ID": project,
            "ANCHOR_PROJECT_ROOT": str(target),
            "ANCHOR_RUNTIME_INSTANCE_ID": runtime,
        }
        self.project = project
        self.target = target
        self.runtime = runtime
        self._next_id = 0
        self._stderr = tempfile.TemporaryFile(  # noqa: SIM115 - closed by stop()
            mode="w+t", encoding="utf-8"
        )
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-c", _WORKER],
            cwd=target,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            start_new_session=True,
        )
        ready = self._read()
        if ready.get("kind") != "READY":
            self._fail(f"worker did not become ready: {ready}")
        assert ready["tools"] == ["anchor_execute", "anchor_help"]

    def _read(self) -> dict:
        assert self.process.stdout is not None
        readable, _, _ = select.select([self.process.stdout], [], [], _TIMEOUT)
        if not readable:
            self._fail("worker response timed out")
        line = self.process.stdout.readline()
        if not line:
            self._fail("worker exited without a response")
        return json.loads(line)

    def _fail(self, message: str) -> None:
        code = self.process.poll()
        self._stderr.flush()
        self._stderr.seek(0)
        stderr = self._stderr.read()[-4000:]
        self._stderr.seek(0, os.SEEK_END)
        raise AssertionError(
            f"{message}; project={self.project}; runtime={self.runtime}; returncode={code}; stderr_tail={stderr!r}"
        )

    def envelope(self, action: str, args: dict | None = None) -> dict:
        if self.process.poll() is not None:
            self._fail("request sent to exited worker")
        self._next_id += 1
        request = {
            "kind": "STEP",
            "id": self._next_id,
            "action": action,
            "args": args or {},
        }
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        response = self._read()
        assert response.get("id") == self._next_id
        if response["kind"] == "CLIENT_ERROR":
            self._fail(f"FastMCP client error {response['error_type']}: {response['message']}")
        assert response["kind"] == "RESULT"
        return response["envelope"]

    def call(self, action: str, args: dict | None = None) -> dict:
        envelope = self.envelope(action, args)
        if not envelope["ok"]:
            raise RemoteCallError(envelope["error"]["message"])
        return envelope["result"]

    def call_error(self, action: str, args: dict | None = None) -> str:
        envelope = self.envelope(action, args)
        assert envelope["ok"] is False
        return envelope["error"]["message"]

    def stop(self, *, crash: bool = False) -> None:
        if self.process.poll() is not None:
            self._stderr.close()
            return
        if not crash:
            try:
                self._next_id += 1
                assert self.process.stdin is not None
                self.process.stdin.write(json.dumps({"kind": "STOP", "id": self._next_id}) + "\n")
                self.process.stdin.flush()
                response = self._read()
                assert response == {"kind": "STOPPED", "id": self._next_id}
                self.process.wait(timeout=5)
                self._stderr.close()
                return
            except (AssertionError, BrokenPipeError, subprocess.TimeoutExpired):
                pass
        os.killpg(self.process.pid, signal.SIGKILL if crash else signal.SIGTERM)
        self.process.wait(timeout=5)
        self._stderr.close()


def _start_task(worker: StdioWorker, name: str, *, selects_memory: bool) -> dict:
    for action in ("status", "memory", "audit_history", "orient"):
        worker.call(action)
    worker.call("new_session", {"name": name, "inline": True})
    task_args = {
        "arg0": (
            "Qualify alpha crash restart memory selection isolation."
            if selects_memory
            else "Qualify beta independent MCP lifecycle."
        ),
        "goal": (
            "Preserve alpha crash restart selection authority."
            if selects_memory
            else "Complete beta while alpha is interrupted."
        ),
        **_TASK,
    }
    task = worker.call("task", task_args)
    for skill in task["required_skills"]:
        if skill["path"] != "n/a":
            worker.call("skill_loaded", {"arg0": skill["skill"]})
    context = task["memory_context"]
    assert bool(context["selection_count"]) is selects_memory
    return task


def _gate(worker: StdioWorker) -> dict:
    worker.call("review")
    return worker.call("gate")


def _assess(worker: StdioWorker, note: str) -> dict:
    return worker.call(
        "learning",
        {
            "arg0": "assess",
            "outcome": "nothing_reusable_learned",
            "notes": note,
        },
    )


def _rows(db: Path, query: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        return connection.execute(query, params).fetchall()


def _lifecycle_digest(db: Path, selection_id: str) -> tuple:
    tables = (
        "memory_selections",
        "memory_dispositions",
        "memory_selection_abandonments",
        "memory_selection_recoveries",
    )
    return tuple(
        _rows(db, f"SELECT * FROM {table} WHERE selection_id=? ORDER BY 1", (selection_id,)) for table in tables
    )


def _write_managed_fixture(home: Path, project: str) -> None:
    artifact = home / "workspace" / "projects" / project
    problems = artifact / "problems"
    work_items = artifact / "work_items"
    problems.mkdir(exist_ok=True)
    work_items.mkdir(exist_ok=True)
    (problems / "PRB-2026-0001.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            problem_id: "PRB-2026-0001"
            project_id: "{project}"
            title: "{project} qualification problem"
            status: "scoped"
            stage: 7
            rigor_level: "full"
            revision: 1
            created_at: "2026-09-10T00:00:00+00:00"
            updated_at: "2026-09-10T00:00:00+00:00"
            next_action: "Run the isolated qualification."
            ---

            # PRB-2026-0001: {project} qualification problem
            """
        ),
        encoding="utf-8",
    )
    (work_items / "WI-2026-0001.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            work_item_id: "WI-2026-0001"
            status: "draft"
            created_at: "2026-09-10T00:00:00+00:00"
            updated_at: "2026-09-10T00:00:00+00:00"
            revision: 1
            implementation_disposition: "unknown"
            reopening_triggers: []
            approvals: []
            provider_bindings: []
            publication_receipts: []
            revisions: []
            ---

            # WI-2026-0001: {project} qualification work item

            ## Outcome
            Qualify only project {project}.

            ## Context
            Fixed routing evidence for project {project}.

            ## Scope
            Read this project-local fixture.

            ## Non-goals
            No cross-project access.

            ## Acceptance criteria
            ["The result belongs to project {project}."]

            ## Implementation notes
            Test fixture only.

            ## Validation notes
            Assert the bound artifact path.

            ## Risks
            Cross-project routing.

            ## Dependencies
            None.

            ## Problem Record
            PRB-2026-0001

            ## Specification
            None.

            ## PR
            [Not captured]

            ## Publication plan
            []
            """
        ),
        encoding="utf-8",
    )


@pytest.mark.timeout(180)
def test_two_live_mcp_servers_isolate_crash_rebind_and_recovery(tmp_path):
    """Compose the routing, learning, continuity, recovery, and DB contracts."""
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.codebase._task_authority import close_accepted_task
    from odibi_anchor.codebase.memory_context import append_memory

    home = tmp_path / "anchor-home"
    alpha_target = tmp_path / "alpha-target"
    beta_target = tmp_path / "beta-target"
    home.mkdir()
    alpha_target.mkdir()
    beta_target.mkdir()
    (alpha_target / "sentinel.txt").write_text("alpha\n", encoding="utf-8")
    (beta_target / "sentinel.txt").write_text("beta\n", encoding="utf-8")
    project_action(home, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(home, "create", name="beta", target=beta_target, output_format="dict")
    _write_managed_fixture(home, "alpha")
    _write_managed_fixture(home, "beta")
    db = home / ".agent_memory.db"
    memory = append_memory(
        alpha_target,
        entry_type="gotcha",
        content=(
            "Alpha crash restart memory selection isolation requires exact durable task "
            "authority and explicit disposition after recovery."
        ),
        tags=["alpha", "crash", "restart", "selection"],
        project="alpha",
        db_path=str(db),
    )

    workers: list[StdioWorker] = []

    def start(project: str, target: Path, runtime: str) -> StdioWorker:
        worker = StdioWorker(project=project, target=target, home=home, runtime=runtime)
        workers.append(worker)
        return worker

    alpha = start("alpha", alpha_target, "alpha-initial")
    beta = start("beta", beta_target, "beta-live")
    try:
        for worker, expected_target in ((alpha, alpha_target), (beta, beta_target)):
            runtime = worker.call("status")["runtime"]
            binding = runtime["route_binding"]
            assert runtime["active_project"] == worker.project
            assert Path(runtime["target_root"]) == expected_target.resolve()
            assert binding["project_id"] == worker.project
            assert binding["runtime_instance_id"] == worker.runtime
            problems = worker.call("problem")["problems"]
            assert [problem["title"] for problem in problems] == [f"{worker.project} qualification problem"]
            item = worker.call("work_item", {"arg0": "show", "work_item_id": "WI-2026-0001"})
            assert item["title"] == f"{worker.project} qualification work item"
            assert Path(item["artifact_path"]).is_relative_to(home / "workspace" / "projects" / worker.project)
        selected = alpha.call("project", {"arg0": "use", "arg1": "beta"})
        assert selected["active_project"] == "beta"
        assert selected["runtime_binding_unchanged"] is True
        assert selected["route_binding"]["project_id"] == "alpha"

        alpha_task = _start_task(alpha, "alpha_interrupted", selects_memory=True)
        alpha_window = alpha_task["accepted_task_authority"]["task_window_id"]
        alpha_selection = alpha_task["memory_context"]["selections"][0]
        assert alpha_selection["memory_id"] == memory["id"]
        _gate(alpha)

        beta_task = _start_task(beta, "beta_independent", selects_memory=False)
        beta_window = beta_task["accepted_task_authority"]["task_window_id"]
        _gate(beta)
        active = _rows(
            db,
            "SELECT project_id,task_window_id FROM learning_obligations "
            "WHERE owner_state='owned' AND status='active' ORDER BY project_id",
        )
        assert active == [("alpha", alpha_window), ("beta", beta_window)]

        selector = home / "workspace" / ".active_project"
        for value in ("alpha", "../invalid-selector", "beta"):
            selector.write_text(value + "\n", encoding="utf-8")
            assert alpha.call("status")["runtime"]["active_project"] == "alpha"
            assert beta.call("status")["runtime"]["active_project"] == "beta"

        alpha.stop(crash=True)
        assert _rows(
            db,
            "SELECT selection_id FROM memory_selections WHERE selection_id=?",
            (alpha_selection["selection_id"],),
        ) == [(alpha_selection["selection_id"],)]
        assert (
            _rows(
                db,
                "SELECT disposition_id FROM memory_dispositions WHERE selection_id=?",
                (alpha_selection["selection_id"],),
            )
            == []
        )

        beta_assessment = _assess(beta, "Beta completed while the independent alpha process was interrupted.")
        assert beta_assessment["assessment"]["obligation_id"]
        assert beta.call("status")["runtime"]["active_project"] == "beta"
        assert _rows(
            db,
            "SELECT project_id,task_window_id,status FROM learning_obligations "
            "WHERE task_window_id IN (?,?) ORDER BY project_id",
            (alpha_window, beta_window),
        ) == [
            ("alpha", alpha_window, "active"),
            ("beta", beta_window, "assessed"),
        ]

        alpha = start("alpha", alpha_target, "alpha-rebound")
        rebound = alpha.call("task_rebind")
        assert rebound["task_window_id"] == alpha_window
        pending = alpha.call("memory", {"arg0": "recovery", "action": "inspect"})
        by_id = {row["selection_id"]: row for row in pending["states"]}
        assert by_id[alpha_selection["selection_id"]]["state"] == "active_pending"
        denied = alpha.call_error(
            "memory",
            {
                "arg0": "recovery",
                "action": "declare_abandoned",
                "selection_id": alpha_selection["selection_id"],
                "prior_task_window_id": alpha_window,
                "reason": {"cause": "process crash"},
                "evidence": {"authority": "task remains open"},
            },
        )
        assert denied == "memory selection recovery is unavailable for the current exact owner"
        assert _rows(db, "SELECT * FROM memory_selection_abandonments") == []
        alpha.call(
            "memory",
            {
                "arg0": "disposition",
                "selection_id": alpha_selection["selection_id"],
                "disposition": "irrelevant",
                "reason": {"qualification": "live task rebound exactly"},
            },
        )
        _assess(alpha, "The live interrupted task rebound without abandonment.")

        terminal_task = _start_task(alpha, "alpha_terminal_crash", selects_memory=True)
        terminal_window = terminal_task["accepted_task_authority"]["task_window_id"]
        terminal_selection = terminal_task["memory_context"]["selections"][0]
        alpha.stop(crash=True)
        closure = close_accepted_task(db, task_window_id=terminal_window, terminal_status="blocked")
        assert closure["status"] == "closed"

        alpha = start("alpha", alpha_target, "alpha-recovery")
        replacement_task = _start_task(alpha, "alpha_replacement", selects_memory=True)
        replacement_window = replacement_task["accepted_task_authority"]["task_window_id"]
        before = alpha.call("memory", {"arg0": "recovery", "action": "inspect"})
        before_states = {row["selection_id"]: row["state"] for row in before["states"]}
        assert before_states[terminal_selection["selection_id"]] == "terminal_unresolved"
        assert before["counts"]["active_pending"] == 1
        assert before["selected_never_applied"] >= 2

        foreign = beta.call_error(
            "memory",
            {
                "arg0": "recovery",
                "action": "declare_abandoned",
                "selection_id": terminal_selection["selection_id"],
                "prior_task_window_id": terminal_window,
                "reason": {"cause": "foreign attempt"},
                "evidence": {"project": "beta"},
            },
        )
        assert foreign == "memory selection recovery is unavailable for the current exact owner"
        wrong_task = alpha.call_error(
            "memory",
            {
                "arg0": "recovery",
                "action": "declare_abandoned",
                "selection_id": terminal_selection["selection_id"],
                "prior_task_window_id": alpha_window,
                "reason": {"cause": "wrong task"},
                "evidence": {"authority": "mismatch"},
            },
        )
        assert wrong_task == "memory selection recovery is unavailable for the current exact owner"

        abandonment_args = {
            "arg0": "recovery",
            "action": "declare_abandoned",
            "selection_id": terminal_selection["selection_id"],
            "prior_task_window_id": terminal_window,
            "reason": {"cause": "authoritative blocked task after process crash"},
            "evidence": {"closure_event_id": closure["event_id"]},
        }
        abandoned = alpha.call("memory", abandonment_args)
        replayed = alpha.call("memory", abandonment_args)
        assert replayed["abandonment_id"] == abandoned["abandonment_id"]
        after_abandonment = alpha.call("memory", {"arg0": "recovery", "action": "inspect"})
        assert after_abandonment["counts"]["abandoned"] == 1
        digest = _lifecycle_digest(db, terminal_selection["selection_id"])
        conflict = alpha.call_error(
            "memory",
            {**abandonment_args, "reason": {"cause": "changed replay payload"}},
        )
        assert "idempotency conflict" in conflict
        assert _lifecycle_digest(db, terminal_selection["selection_id"]) == digest

        recovery_args = {
            "arg0": "recovery",
            "action": "recover",
            "selection_id": terminal_selection["selection_id"],
            "prior_task_window_id": terminal_window,
            "reason": {"purpose": "continue exact alpha qualification"},
            "evidence": {"replacement_task_window_id": replacement_window},
        }
        recovered = alpha.call("memory", recovery_args)
        assert alpha.call("memory", recovery_args)["recovery_id"] == recovered["recovery_id"]
        after_recovery = alpha.call("memory", {"arg0": "recovery", "action": "inspect"})
        assert after_recovery["counts"]["recovered"] == 1
        assert (
            _rows(
                db,
                "SELECT disposition_id FROM memory_dispositions WHERE selection_id=?",
                (terminal_selection["selection_id"],),
            )
            == []
        )

        alpha.call(
            "memory",
            {
                "arg0": "disposition",
                "all_pending": True,
                "disposition": "irrelevant",
                "reason": {"qualification": "explicit post-recovery decision"},
            },
        )
        disposed = alpha.call("memory", {"arg0": "recovery", "action": "inspect"})
        assert disposed["counts"]["semantically_disposed"] >= 2
        _gate(alpha)
        _assess(alpha, "Recovered alpha selections were explicitly reconciled.")

        alpha_artifact = home / "workspace" / "projects" / "alpha"
        beta_artifact = home / "workspace" / "projects" / "beta"
        assert alpha_artifact != beta_artifact
        assert (alpha_artifact / "continuity" / "v1" / "OWNER.json").is_file()
        assert (beta_artifact / "continuity" / "v1" / "OWNER.json").is_file()
        owners = _rows(
            db,
            "SELECT DISTINCT project_id FROM accepted_task_records "
            "WHERE task_window_id IN (?,?,?,?) ORDER BY project_id",
            (alpha_window, beta_window, terminal_window, replacement_window),
        )
        assert owners == [("alpha",), ("beta",)]

        descriptor = alpha_artifact / "PROJECT.md"
        descriptor.write_text(
            descriptor.read_text(encoding="utf-8").replace(
                str(alpha_target.resolve()), str((tmp_path / "retargeted-alpha").resolve())
            ),
            encoding="utf-8",
        )
        stale = alpha.call_error("status")
        assert "bound managed project 'alpha' target root changed" in stale
        assert "re-run init()" in stale.lower()
    finally:
        for worker in reversed(workers):
            worker.stop()
