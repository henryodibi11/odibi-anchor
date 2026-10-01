"""Public-dispatcher regressions for the audited failures.

Each test drives the real `anchor(...)` dispatcher (or the public helper the
dispatcher itself calls) rather than an internal function, because every one of
these defects was invisible from the internal API and only appeared on the public
path:

  - `memory_context(memory_id=..., output_format="markdown")` rendered correctly
    when called directly, but `anchor("memory", ...)` raised `KeyError: 'summary'`
    because the dispatcher forced dict and then used the relevance-query renderer.
  - `child_environment` stripped every `ANCHOR_` name, which silently disabled
    `ANCHOR_REQUIRE_INSTALLED_QUALIFICATION` — a control the quality workflow
    exports and the installed-distribution suite reads.
  - `_parse` demanded `rigor_level` although the reader still carries a `"full"`
    default, so Problem Records that had always parsed started failing.
  - `problem list` reported malformed records only in its dict result, so the
    markdown renderer said "No Problem Records." while corruption sat on disk.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._managed_record_schema import (
    managed_record_schemas,
    missing_required_fields,
    scan_managed_records,
)
from odibi_anchor._dispatcher._problem import (
    _empty_record,
    _parse,
    _render,
    problem_action,
    render_problem_result,
)
from odibi_anchor.codebase._memory_db import insert_memory
from odibi_anchor.codebase.memory_context import memory_context, render_memory_report
from odibi_anchor.pytest_runner import ANCHOR_ENV_PREFIX, child_environment

MISSING_ID = "00000000-0000-0000-0000-000000000000"

# Expectations stated as literals, not imported, so these tests fail on their own
# assertions against a build that lacks the symbols rather than erroring at
# collection and reporting nothing.
EXPECTED_PRESERVED = ("ANCHOR_OFFLINE_WHEELHOUSE", "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION")
EXPECTED_DEFAULTED = ("next_action", "revision", "rigor_level", "stage")


def _preserved_env_names():
    from odibi_anchor.pytest_runner import PRESERVED_ENV_NAMES

    return PRESERVED_ENV_NAMES


def _reader_defaults():
    from odibi_anchor._dispatcher._problem import READER_DEFAULTS

    return READER_DEFAULTS


# ── 1. markdown rendering of an id lookup ────────────────────────────────────


@pytest.fixture
def memory_store(tmp_path):
    db = str(tmp_path / "memory.db")
    created = insert_memory(
        db, project="alpha", type="discovery", content="An entry to resolve.",
        related_files=[], tags=[], source="test", confidence=0.4, status="candidate",
    )
    return db, created["id"]


def _dispatch_memory(root, db, *, memory_id, output_format, project="alpha"):
    """Drive memory_action exactly as the dispatcher wires it."""
    from odibi_anchor._dispatcher._memory_actions import memory_action

    class _State:
        active_project = project
        task_window_id = None

    return memory_action(
        root, (), {"memory_id": memory_id, "output_format": output_format, "db_path": db},
        session_state=_State(), query_fn=memory_context, render_fn=render_memory_report,
    )


def test_markdown_lookup_of_a_found_id_renders(tmp_path, memory_store):
    db, memory_id = memory_store
    rendered = _dispatch_memory(tmp_path, db, memory_id=memory_id, output_format="markdown")
    assert isinstance(rendered, str), "markdown must not fall back to a dict"
    assert memory_id in rendered
    assert "An entry to resolve." in rendered


def test_markdown_lookup_of_a_missing_id_renders(tmp_path, memory_store):
    """The miss path raised the same KeyError, so it needs its own regression."""
    db, _ = memory_store
    rendered = _dispatch_memory(tmp_path, db, memory_id=MISSING_ID, output_format="markdown")
    assert isinstance(rendered, str)
    assert "Not found" in rendered


def test_dict_lookup_is_unchanged(tmp_path, memory_store):
    """Negative control: the renderer selection must not disturb dict output."""
    db, memory_id = memory_store
    result = _dispatch_memory(tmp_path, db, memory_id=memory_id, output_format="dict")
    assert isinstance(result, dict)
    assert result["kind"] == "memory_entry"
    assert result["found"] is True


def test_the_query_renderer_still_rejects_an_entry_payload():
    """Why two renderers are needed: the shapes are genuinely incompatible."""
    with pytest.raises(KeyError):
        render_memory_report({"kind": "memory_entry", "found": False, "entries": [], "count": 0})


def test_entry_renderer_is_public_so_the_dispatcher_can_select_it():
    from odibi_anchor.codebase.memory_context import render_memory_entry_report

    text = render_memory_entry_report(
        {"kind": "memory_entry", "memory_id": MISSING_ID, "found": False,
         "entries": [], "count": 0, "reason": "no retrievable memory entry"},
    )
    assert "Not found" in text


# ── 2. preserved suite controls vs stripped routing ──────────────────────────


@pytest.mark.parametrize("name", EXPECTED_PRESERVED)
def test_suite_controls_survive_the_child_environment(name):
    env = child_environment({name: "1", "ANCHOR_HOME": "/operator/home"})
    assert env.get(name) == "1", f"{name} is a suite control, not routing"


def test_required_installed_qualification_is_preserved_by_name():
    """The quality workflow exports this; losing it disables the gate silently."""
    assert "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION" in _preserved_env_names()


def test_offline_wheelhouse_is_preserved_by_name():
    assert "ANCHOR_OFFLINE_WHEELHOUSE" in _preserved_env_names()


def test_routing_is_still_stripped():
    """Negative control: preserving controls must not reopen the routing leak."""
    env = child_environment({
        "ANCHOR_HOME": "/operator/home",
        "ANCHOR_MEMORY_DB": "/operator/home/.agent_memory.db",
        "ANCHOR_PROJECT_ID": "operator-project",
        "ANCHOR_PROJECT_ROOT": "/operator/project",
        "ANCHOR_DURABLE_ROOT": "/operator/durable",
    })
    leaked = sorted(
        key for key in env
        if key.startswith(ANCHOR_ENV_PREFIX) and key not in _preserved_env_names()
    )
    assert not leaked, f"routing variables reached the child: {leaked}"


def test_preserved_names_are_not_routing():
    """Guard against someone later adding a project/home selector to the exemption."""
    for name in _preserved_env_names():
        for routing_word in ("HOME", "PROJECT", "MEMORY_DB", "ROOT", "AUTHORITY"):
            assert routing_word not in name, f"{name} looks like routing"


def test_quality_workflow_still_exports_the_qualification_control():
    """If the workflow stops setting it, this exemption is pointless."""
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "quality.yml"
    assert "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION" in workflow.read_text(encoding="utf-8")


# ── 3. Problem Record reader defaults ────────────────────────────────────────


def _problem_without(tmp_path, *omit):
    meta = {
        "problem_id": "PRB-2026-0001", "project_id": "p", "title": "T",
        "status": "framing", "stage": 1, "rigor_level": "full", "revision": 1,
        "created_at": "x", "updated_at": "y", "next_action": "z",
    }
    for field in omit:
        meta.pop(field)
    directory = tmp_path / "problems"
    directory.mkdir(exist_ok=True)
    path = directory / "PRB-2026-0001.md"
    body = "".join(f"{k}: {json.dumps(v)}\n" for k, v in meta.items())
    path.write_text(f"---\n{body}---\n# x\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("field", EXPECTED_DEFAULTED)
def test_a_record_omitting_a_defaulted_field_still_parses(tmp_path, field):
    record = _parse(_problem_without(tmp_path, field))
    assert record["meta"][field] is not None or field == "next_action"


def test_the_documented_rigor_default_is_applied(tmp_path):
    record = _parse(_problem_without(tmp_path, "rigor_level"))
    assert record["meta"]["rigor_level"] == "full"


def test_defaulted_fields_are_not_reported_as_malformed(tmp_path):
    """The scanner must agree with what a read actually does."""
    _problem_without(tmp_path, "rigor_level")
    assert scan_managed_records(tmp_path) == []


def test_a_truly_absent_field_is_still_rejected(tmp_path):
    """Negative control: defaults must not blanket-disable the #15 contract."""
    with pytest.raises(ValueError) as excinfo:
        _parse(_problem_without(tmp_path, "problem_id"))
    assert "problem_id" in excinfo.value.context["missing_fields"]


def test_schema_defaults_are_derived_from_the_reader():
    schema = managed_record_schemas()["problem"]
    assert schema.defaulted_fields == frozenset(_reader_defaults())
    assert schema.defaulted_fields <= schema.required_fields


def test_canonical_record_round_trips(tmp_path):
    directory = tmp_path / "problems"
    directory.mkdir()
    path = directory / "PRB-2026-0002.md"
    path.write_text(_render(_empty_record("PRB-2026-0002", "p", "T", "full")), encoding="utf-8")
    assert _parse(path)["meta"]["problem_id"] == "PRB-2026-0002"
    assert missing_required_fields(_parse(path)["meta"], managed_record_schemas()["problem"]) == []


# ── 4. malformed diagnostics reach the markdown renderer ─────────────────────


@pytest.fixture
def malformed_problem_root(tmp_path):
    directory = tmp_path / "problems"
    directory.mkdir()
    (directory / "PRB-2026-0001.md").write_text(
        '---\nid: "PRB-2026-0001"\ntitle: "Some problem"\n---\n# Content\n', encoding="utf-8",
    )
    return tmp_path


def _list_markdown(root):
    result = problem_action(
        artifact_root=str(root), project_id="p", args=("list",), output_format="dict",
    )
    return result, render_problem_result("list", result)


def test_markdown_listing_names_the_malformed_record(malformed_problem_root):
    _, text = _list_markdown(malformed_problem_root)
    assert "PRB-2026-0001.md" in text
    assert "Malformed records" in text


def test_markdown_listing_shows_the_missing_fields(malformed_problem_root):
    _, text = _list_markdown(malformed_problem_root)
    assert "problem_id" in text


def test_markdown_listing_shows_the_repair_call(malformed_problem_root):
    _, text = _list_markdown(malformed_problem_root)
    assert 'anchor("problem", "create"' in text


def test_markdown_listing_does_not_claim_the_directory_is_empty(malformed_problem_root):
    """The reported hazard: corruption on disk reported as "No Problem Records."."""
    _, text = _list_markdown(malformed_problem_root)
    assert "No Problem Records." not in text
    assert "No readable Problem Records." in text


def test_a_clean_directory_still_reports_plainly(tmp_path):
    """Negative control: the new branch must not fire when nothing is wrong."""
    (tmp_path / "problems").mkdir()
    _, text = _list_markdown(tmp_path)
    assert "No Problem Records." in text
    assert "Malformed records" not in text


def test_valid_records_are_still_listed_alongside_malformed(malformed_problem_root):
    directory = malformed_problem_root / "problems"
    (directory / "PRB-2026-0002.md").write_text(
        _render(_empty_record("PRB-2026-0002", "p", "A real problem", "full")), encoding="utf-8",
    )
    result, text = _list_markdown(malformed_problem_root)
    assert result["count"] == 1
    assert "PRB-2026-0002" in text
    assert "Malformed records" in text


# ── the installed-wheel qualification contract itself ────────────────────────


def test_installed_distribution_reads_both_controls():
    """These names are the contract between the workflow and the suite."""
    source = (
        Path(__file__).resolve().parents[1] / "test_installed_distribution.py"
    ).read_text(encoding="utf-8")
    assert "ANCHOR_OFFLINE_WHEELHOUSE" in source
    assert "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION" in source


def test_child_environment_keeps_the_summary_handoff():
    """Unchanged behaviour, asserted so the exemption logic cannot break it."""
    from odibi_anchor.pytest_runner import SUMMARY_ENV

    env = child_environment({SUMMARY_ENV: "/tmp/s.json", "ANCHOR_HOME": "/operator"})
    assert env[SUMMARY_ENV] == "/tmp/s.json"


def test_child_environment_does_not_mutate_the_caller():
    base = {"ANCHOR_HOME": "/operator", "ANCHOR_OFFLINE_WHEELHOUSE": "/wheels"}
    snapshot = dict(base)
    child_environment(base)
    assert base == snapshot


def test_subprocess_child_really_sees_the_preserved_control():
    """End-to-end: the exemption must survive an actual process boundary."""
    env = child_environment({
        "PATH": "/usr/bin:/bin",
        "ANCHOR_HOME": "/operator/home",
        "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION": "1",
    })
    completed = subprocess.run(
        [sys.executable, "-c",
         "import os,json;print(json.dumps({k:v for k,v in os.environ.items() "
         "if k.startswith('ANCHOR_')}))"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    seen = json.loads(completed.stdout)
    assert seen == {"ANCHOR_REQUIRE_INSTALLED_QUALIFICATION": "1"}

