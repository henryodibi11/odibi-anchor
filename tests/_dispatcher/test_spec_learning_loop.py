"""S-4: spec↔production learning loop — link learnings to a spec, resurface them."""
from odibi_anchor._dispatcher._auto_confirm import (
    inject_spec_tag,
)
from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch


class _State:
    prior_learn_debt = False
    active_task_mode = None
    spec_persisted = False
    spec_review_rating = None
    active_spec_name = None

    def __init__(self):
        self.skills_loaded = set()
        self.skill_hints_emitted = set()


# Tests use explicit db_path — the module default-db is a global the function/
# submodule name-collision makes hard to monkeypatch reliably.


# ── post_dispatch records the active spec name ──
def test_post_dispatch_sets_active_spec_name_from_name():
    st = _State()
    run_post_dispatch("spec", {"created": True, "path": "/x/ORDERS_SPEC.md", "name": "ORDERS"},
                      None, (), {}, session_timings=[], session_files_changed=set(),
                      session_state=st, planning_required_actions=frozenset())
    assert st.active_spec_name == "ORDERS"


def test_post_dispatch_derives_spec_name_from_path():
    st = _State()
    run_post_dispatch("spec", {"updated": True, "path": "/x/BILLING_SPEC.md"},
                      None, (), {}, session_timings=[], session_files_changed=set(),
                      session_state=st, planning_required_actions=frozenset())
    assert st.active_spec_name == "BILLING"


def test_post_dispatch_sets_active_problem_and_offers_candidate_evidence():
    st = _State()
    run_post_dispatch(
        "problem",
        {"problem_id": "PRB-2026-0001"},
        None,
        (),
        {},
        session_timings=[],
        session_files_changed=set(),
        session_state=st,
        planning_required_actions=frozenset(),
    )
    result = {"summary": "CPU remained above 95%."}
    run_post_dispatch(
        "profile_table",
        result,
        None,
        (),
        {},
        session_timings=[],
        session_files_changed=set(),
        session_state=st,
        planning_required_actions=frozenset(),
    )

    assert st.active_problem == "PRB-2026-0001"
    assert result["problem_evidence_candidate"] == {
        "problem_id": "PRB-2026-0001",
        "source": "anchor:profile_table",
        "observation": "CPU remained above 95%.",
        "persisted": False,
    }


# ── anchor("save") spec tagging (S-4) ──
def test_inject_spec_tag_appends():
    kw = {"tags": ["manual"]}
    inject_spec_tag(kw, "ORDERS")
    assert "spec:ORDERS" in kw["tags"] and "manual" in kw["tags"]


def test_inject_spec_tag_creates_tags_list():
    kw = {}
    inject_spec_tag(kw, "ORDERS")
    assert kw["tags"] == ["spec:ORDERS"]


def test_inject_spec_tag_noop_without_spec():
    kw = {"tags": ["manual"]}
    inject_spec_tag(kw, None)
    assert kw["tags"] == ["manual"]


def test_inject_spec_tag_no_duplicate():
    kw = {"tags": ["spec:ORDERS"]}
    inject_spec_tag(kw, "ORDERS")
    assert kw["tags"].count("spec:ORDERS") == 1
