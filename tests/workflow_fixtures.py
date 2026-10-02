"""Explicit workflow drafts and historical records for disposable integration targets.

These seed storage fixtures, not alternate production admission. Public workflow
creation and fresh task rejection have their own dispatcher contract tests.
"""

import copy
import os
import uuid
from pathlib import Path
from unittest.mock import patch


def init_source_runtime(*, root=None, route_binding=None, **kwargs):
    """Bootstrap a disposable managed fixture in its explicitly declared test domain."""
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    if route_binding is not None:
        with patch.dict(os.environ, ANCHOR_TRUST_DOMAIN=os.environ.get("ANCHOR_TRUST_DOMAIN") or "personal"):
            return init(route_binding=route_binding, **kwargs)
    home = Path(os.environ["ANCHOR_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    if not (home / "workspace/projects/source-tests/PROJECT.md").exists():
        project_action(home, "create", name="source-tests", target=root, output_format="dict")
    with patch.dict(os.environ, ANCHOR_TRUST_DOMAIN=os.environ.get("ANCHOR_TRUST_DOMAIN") or "personal"):
        return init(root=root, project="source-tests", **kwargs)


def source_workflow_kwargs(paths, *, risk="low"):
    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor._dispatcher._workflow_admission import workflow_owner
    from odibi_anchor._utils._session_state import _SESSION_STATE
    from odibi_anchor.codebase._workflow import create_workflow

    state = copy.copy(_SESSION_STATE)
    state.trust_domain = os.environ.get("ANCHOR_TRUST_DOMAIN") or "personal"
    plan = {
        "schema_version": 1, "goal": "Qualify the isolated source regression fixture",
        "risk": risk, "execution_mode": "source_change", "scope": list(paths),
        "source_paths": list(paths), "exclusions": [], "constraints": [], "risks": [],
        "stop_conditions": [], "unresolved_decisions": [],
        "criteria": [{"id": "regression", "expected": "Source regression contract holds", "method": "pytest",
                      "test_targets": ["tests/"]}],
        "destination": {"kind": "github_ref", "repository": "fixture/never-published", "ref": "refs/heads/main"},
    }
    draft = create_workflow(_ENV["memory_db"], owner=workflow_owner(state),
                            request_id=uuid.uuid4().hex, plan=plan)
    return {"workflow_id": draft["workflow_id"], "trust_domain": state.trust_domain}


def accept_fixture_plan(anchor):
    state = anchor("workflow", output_format="dict")["state"]
    return anchor("workflow", "accept_plan", expected_generation=state["generation"],
                  request_id="accept-fixture", output_format="dict")


def seed_legacy_source_task(anchor, description, **kwargs):
    """Seed pre-enrollment accepted state; callers exercise the real rebind path.

    No fresh task is accepted and no enrollment guard is disabled. Only legacy
    recovery tests should use this historical fixture constructor.
    """
    from odibi_anchor._dispatcher._baseline_qualification import qualify_task_baseline
    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor._repository_snapshot import capture_task_repository_baseline
    from odibi_anchor._utils._session_state import _SESSION_STATE
    from odibi_anchor.codebase._task_authority import persist_accepted_task
    from odibi_anchor.planning import task_execution_context
    from odibi_anchor.planning._task_policy import BpsKernel
    from odibi_anchor.planning._task_profile import normalize_task_profile

    state = copy.copy(_SESSION_STATE)
    state.task_window_id = "ltw_" + uuid.uuid4().hex
    state.active_task_mode = "implementation"
    state.active_task_profile = normalize_task_profile(legacy_mode="implementation", risk="low", rigor="direct")
    state.task_goal = kwargs["goal"]
    state.bps_kernel = BpsKernel(description, kwargs["goal"])
    state.trust_domain = os.environ.get("ANCHOR_TRUST_DOMAIN")
    state.workflow_binding = None
    state.task_repository_baseline = capture_task_repository_baseline(state.target_root, "main")
    state.task_repository_baseline_qualification = qualify_task_baseline(
        state.task_repository_baseline, task_window_id=state.task_window_id,
        execution_mode="source_change", target_root=state.target_root,
    )
    result = task_execution_context(description, goal=kwargs["goal"], mode="implementation",
                                    risk="low", rigor="direct", output_format="dict",
                                    acceptance_criteria=kwargs["acceptance_criteria"],
                                    **{key: kwargs[key] for key in ("background", "in_scope", "out_of_scope")
                                       if key in kwargs})
    state.task_handoff_context = {key: result[key] for key in (
        "intent", "background", "scope", "constraints", "verification", "guardrails", "risks", "resources",
    ) if key in result}
    authority = persist_accepted_task(_ENV["memory_db"], session_state=state,
                                     task_stage={"trust_domain": state.trust_domain,
                                                 "repository_scope": kwargs.get("repository_scope", ())},
                                     task_result=result)
    # Represent the original live producer, not an already-rebound session.
    # No rebind event or fresh public acceptance is manufactured by this fixture.
    vars(_SESSION_STATE).update(vars(state))
    return {**result, "accepted_task_authority": authority, "task_window_id": state.task_window_id}
