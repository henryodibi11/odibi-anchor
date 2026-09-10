"""D-004: 1-10-100 advisory when a post-hoc diagnostic runs without a prior pre-check."""
from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch


class _State:
    prior_learn_debt = False
    active_task_mode = "debugging"
    spec_persisted = False
    spec_review_rating = None
    skills_loaded = set()
    skill_hints_emitted = set()


def _run(action, result, timings, err=None):
    return run_post_dispatch(
        action, result, err, (), {},
        session_timings=timings,
        session_files_changed=set(),
        session_state=_State(),
        planning_required_actions=frozenset(),
    )


def _t(a, e=None):
    return {"action": a, "error": e}


def test_advisory_added_when_no_precheck():
    timings = [_t("task"), _t("diff")]
    out = _run("diff", {"kind": "diff"}, timings)
    assert any("1-10-100" in s for s in out["suggested_next_actions"])


def test_no_advisory_when_pre_join_ran():
    timings = [_t("task"), _t("pre_join"), _t("debug")]
    out = _run("debug", {"kind": "debug"}, timings)
    assert not any("1-10-100" in s for s in out.get("suggested_next_actions", []))


def test_no_advisory_when_pre_merge_ran():
    timings = [_t("pre_merge"), _t("diagnose_empty")]
    out = _run("diagnose_empty", {"kind": "diagnose_empty"}, timings)
    assert not any("1-10-100" in s for s in out.get("suggested_next_actions", []))


def test_no_advisory_for_non_diagnostic_action():
    out = _run("map", {"kind": "map"}, [_t("map")])
    assert not any("1-10-100" in s for s in out.get("suggested_next_actions", []))


def test_no_advisory_on_error():
    out = _run("diff", {"kind": "diff"}, [_t("diff")], err=RuntimeError("x"))
    # error path returns result unchanged (no advisory)
    assert not any("1-10-100" in s for s in (out or {}).get("suggested_next_actions", []))
