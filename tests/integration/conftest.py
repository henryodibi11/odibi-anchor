"""Fixtures for end-to-end incident replay against a fake Databricks runtime.

See ``tests/integration/README.md``. Every scenario drives public entry points
(``bootstrap_managed_project``, the returned ``anchor`` callable, durability and
portfolio functions, the MCP gateway) in one pytest process; ``FakeDatabricks``
supplies the SDK, compute identity and local disk.
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fixtures.fake_databricks import FakeDatabricks, write_databricks_portfolio

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECT_INSTALLED_VARIABLE = "INCIDENT_REPLAY_EXPECT_INSTALLED"


@pytest.fixture(autouse=True, scope="session")
def _installed_distribution_guard() -> None:
    """Refuse to report wheel qualification while importing the source tree."""
    if os.environ.get(EXPECT_INSTALLED_VARIABLE) != "1":
        return
    import odibi_anchor

    origin = Path(odibi_anchor.__file__).resolve()
    if REPOSITORY_ROOT in origin.parents:
        pytest.exit(
            f"{EXPECT_INSTALLED_VARIABLE}=1 but odibi_anchor was imported from the source "
            f"tree ({origin}); run with -o pythonpath=. --confcutdir=tests/integration",
            returncode=4,
        )


@pytest.fixture(autouse=True)
def _isolated_process() -> Iterator[None]:
    """Give each scenario a process environment with no Anchor bindings, then restore it."""
    saved = dict(os.environ)
    for name in [name for name in os.environ if name.startswith("ANCHOR_")]:
        del os.environ[name]
    try:
        yield
    finally:
        FakeDatabricks.new_process()
        os.environ.clear()
        os.environ.update(saved)


def _startup() -> Any:
    # bootstrap.init evicts odibi_anchor modules; always resolve the current module.
    return importlib.import_module("odibi_anchor.startup")


class ReplayHarness:
    """One disposable portfolio-managed Databricks deployment."""

    def __init__(self, root: Path, databricks: FakeDatabricks) -> None:
        self.root = root
        self.databricks = databricks
        # Persistent Workspace FUSE stand-ins: instructions, portfolio, project targets.
        self.instruction_root = root / "workspace-fuse" / "instructions"
        self.instruction_root.mkdir(parents=True)
        self.config = self.instruction_root / ".odibi-anchor" / "anchor.toml"
        self.config.parent.mkdir()

    def target(self, name: str) -> Path:
        path = self.root / "workspace-fuse" / "targets" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_portfolio(self, projects: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        return write_databricks_portfolio(
            self.config, environment=self.databricks,
            instruction_root=self.instruction_root, projects=projects, **kwargs,
        )

    def bootstrap(self, project_id: str = "alpha", **kwargs: Any) -> dict[str, Any]:
        """Run the managed launcher's public bootstrap for one project."""
        return _startup().bootstrap_managed_project(
            config_path=self.config, project_id=project_id,
            instruction_root=self.instruction_root, **kwargs,
        )

    def create_project(self, project_id: str = "alpha", target: Path | None = None) -> dict[str, Any]:
        """Explicitly approved creation through the managed bootstrap path."""
        if not self.config.exists():
            self.write_portfolio({})
        return self.bootstrap(
            project_id, create_if_missing=True, project_root=target or self.target(project_id),
        )

    @staticmethod
    def start_analysis_task(anchor: Any, name: str = "incident_replay") -> dict[str, Any]:
        """Open one read-only inquiry task through the bound dispatcher."""
        anchor("new_session", name=name, inline=True, output_format="dict")
        return anchor(
            "task",
            "Inspect replayed incident state.",
            goal="Confirm route and continuity survive the replayed incident.",
            mode="analysis",
            trust_domain="work",
            known_facts=["The project is portfolio managed on a fake Databricks host."],
            constraints=["Read-only inspection."],
            acceptance_criteria=["Route and continuity facts are reported."],
            output_format="dict",
        )

    @staticmethod
    def snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
        """Publish live state the way ``anchor state snapshot`` does, outside any task."""
        environment = runtime["preparation"]["environment"]
        return importlib.import_module("odibi_anchor.durability").snapshot_state(
            source_db=environment["ANCHOR_MEMORY_DB"],
            source_artifacts=Path(environment["ANCHOR_HOME"]) / "workspace" / "projects",
            durable_root=environment["ANCHOR_DURABLE_ROOT"],
            authority_id=environment["ANCHOR_AUTHORITY_ID"],
            databricks=True,
        )

    def new_compute(self) -> None:
        self.databricks.new_compute()

    def new_process(self) -> None:
        self.databricks.new_process()


def accept_workflow(call: Any, plan: dict[str, Any], *, name: str) -> str:
    """Draft ``plan`` in an inquiry, close it, bind a producer, and accept the plan.

    ``call(action, *args, **kwargs)`` is any transport that returns the action's
    dictionary result and raises on failure.
    """
    call("new_session", name=f"{name}_inquiry", inline=True)
    call(
        "task", f"Plan {name}.", goal=f"Define the exact {name} change.", mode="analysis",
        risk=plan["risk"], trust_domain="work", known_facts=["Disposable replay fixture."],
        constraints=["Read only."], acceptance_criteria=["The plan names exact paths."],
    )
    workflow_id = call(
        "workflow", "create", plan=plan, request_id=f"{name}-create",
    )["state"]["workflow_id"]
    call("review")
    call("gate")
    call("learning", "assess", outcome="nothing_reusable_learned")
    call("new_session", name=f"{name}_producer", inline=True)
    call(
        "task", f"Perform {name}.", goal=f"Apply the accepted {name} plan.",
        mode="implementation", work_type="change", execution_mode=plan["execution_mode"],
        risk=plan["risk"], trust_domain="work", workflow_id=workflow_id,
        known_facts=["The plan is accepted before any write."],
        constraints=["Only the planned paths."], acceptance_criteria=["The planned change is applied."],
    )
    generation = call("workflow")["state"]["generation"]
    call("workflow", "accept_plan", expected_generation=generation, request_id=f"{name}-accept")
    return workflow_id


def workflow_plan(execution_mode: str, paths: list[str], **overrides: Any) -> dict[str, Any]:
    """A minimal valid low-risk plan for disposable replay fixtures."""
    plan: dict[str, Any] = {
        "schema_version": 1, "goal": "Apply one disposable replay change.", "risk": "low",
        "execution_mode": execution_mode, "scope": list(paths),
        "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
        "unresolved_decisions": [],
        "reconciliation": {"requirements": [], "reason": "Disposable replay fixture."},
        "criteria": [{"id": "replay", "expected": "Replay change applied", "method": "pytest",
                      "test_targets": ["tests/"]}],
    }
    if execution_mode == "artifact_only":
        plan["artifact_paths"] = list(paths)
        plan["destination"] = {"kind": "managed_artifacts"}
    else:
        plan["source_paths"] = list(paths)
        plan["destination"] = {
            "kind": "github_ref", "repository": "fixture/never-published", "ref": "refs/heads/main",
        }
    plan.update(overrides)
    return plan


@pytest.fixture
def databricks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabricks:
    return FakeDatabricks(tmp_path / "databricks").install(monkeypatch)


@pytest.fixture
def replay(tmp_path: Path, databricks: FakeDatabricks) -> ReplayHarness:
    return ReplayHarness(tmp_path / "deployment", databricks)


@pytest.fixture
def workflow() -> SimpleNamespace:
    """``workflow.plan(...)`` builds a disposable plan; ``workflow.accept(call, plan, name=...)`` admits it."""
    return SimpleNamespace(plan=workflow_plan, accept=accept_workflow)
