from __future__ import annotations

import contextlib
import io
from pathlib import Path

from odibi_anchor._dispatcher._problem import problem_action
from odibi_anchor.bootstrap import init
from odibi_anchor.planning._task_profile import normalize_task_profile


def test_cw_operational_actions_render_and_ledger_incident_with_problem(tmp_path):
    from odibi_anchor._utils import _session_state

    prior_state = _session_state._SESSION_STATE
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            anchor, _, _ = init(root=str(tmp_path))
        state = next(
            cell.cell_contents for cell in (anchor.__closure__ or ())
            if type(cell.cell_contents).__name__ == "SessionState"
        )
        state.active_task_profile = normalize_task_profile(execution_mode="artifact_only")
        state.artifact_root = str(tmp_path)
        state.target_root = str(tmp_path)
        state.managed_artifact_ledger = []

        structured = anchor(
            "spark_diagnose", "Exchange hashpartitioning(id)", output_format="dict",
        )
        rendered = anchor("spark_diagnose", "Exchange hashpartitioning(id)")
        assert structured["collector"] == "spark_diagnose"
        assert rendered.startswith("# Spark Diagnose")

        problem = problem_action(
            state.artifact_root, "create", title="Pipeline incident", definition="A test incident",
            output_format="dict",
        )
        incident = anchor(
            "incident_snapshot", problem_id=problem["problem_id"], output_format="dict",
        )
        assert incident["attachment"]["status"] == "attached", incident["attachment"]
        assert [entry.kind for entry in state.managed_artifact_ledger[-2:]] == ["incident_snapshot", "problem"]

        updated = problem_action(state.artifact_root, "show", problem["problem_id"], output_format="dict")
        assert updated["evidence_count"] == 1
        assert (Path(state.artifact_root) / incident["managed_writes"][0]["relative_path"]).is_file()
    finally:
        _session_state._SESSION_STATE = prior_state
