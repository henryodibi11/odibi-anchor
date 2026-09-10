"""Post-dispatch never creates skill obligations outside accepted-task routing."""
from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch
from odibi_anchor._utils._session_state import SessionState, reset_task_policy_state
from odibi_anchor.planning._task_config import _MODE_DEFAULTS


class _State:
    prior_learn_debt = False
    active_task_mode = "implementation"
    spec_persisted = False
    spec_review_rating = None

    def __init__(self, loaded=None):
        self.skills_loaded = set(loaded or [])


def _run(action, result, args=(), kwargs=None, state=None):
    return run_post_dispatch(
        action, result, None, args, kwargs or {},
        session_timings=[{"action": action, "error": None}],
        session_files_changed=set(),
        session_state=state or _State(),
        planning_required_actions=frozenset(),
    )


def _has_hint(result):
    return any("data-operations" in s for s in result.get("suggested_next_actions", []))


def test_read_only_data_actions_do_not_invent_mutation_skill_obligations():
    st = _State()
    for action in ("profile_table", "quality", "schema_diff", "reconcile"):
        out = _run(action, {"kind": action}, state=st)
        assert not _has_hint(out)


def test_task_text_does_not_bypass_accepted_task_profile():
    st = _State()
    out = _run("task", {"kind": "task"}, args=("merge orders into silver",),
               kwargs={"goal": "upsert to delta"}, state=st)
    assert not _has_hint(out)


def test_advisory_output_cannot_create_cross_task_skill_debt():
    state = SessionState()
    assert not hasattr(state, "skill_hints_emitted")
    assert all(
        not action.startswith("SKILL:")
        for action in _MODE_DEFAULTS["testing"]["suggested_next_actions"]
    )
    state.skills_loaded.add("writing-tests")
    reset_task_policy_state(state)
    assert not hasattr(state, "skill_hints_emitted")
