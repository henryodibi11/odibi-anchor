"""Validation errors and help must state what is valid, not only what is wrong.

Covers the discoverability gaps reported in issues #8, #9, #10, #11, #12 and #14:
an agent constructing its first learning capture had to iterate through several
opaque ValueErrors or grep the installed package to find the allowed values.
"""
import pytest

from odibi_anchor._dispatcher._dispatch_table import build_help_text
from odibi_anchor.codebase.structured_learning_context import REFS, _capture_result
from odibi_anchor.planning._task_profile import _LEGACY_DEFAULTS, normalize_task_profile


def _capture_error(**overrides):
    """Drive capture's payload validation and return the raised message.

    `_capture` validates the whole payload before opening the database, so an
    invalid payload raises without any database or obligation state existing. The
    leading underscore keys satisfy the owner lookup that runs first.
    """
    from odibi_anchor.codebase import structured_learning_context as slc

    payload = {
        "_obligation_id": "lob_probe",
        "_project_id": "probe-project",
        "_task_window_id": "ltw_probe",
        "observation_type": "reusable_practice",
        "summary": "A reusable practice worth keeping.",
        "signal_key": "discoverability-probe",
        "evidence": [
            {"reference_type": "file", "reference": "README.md", "summary": "ref"},
        ],
    }
    payload.update(overrides)
    with pytest.raises(ValueError) as excinfo:
        slc._capture(payload)
    return str(excinfo.value)


# ── #8/#9: capture validation errors name the valid values ───────────────────


def test_invalid_applicability_scope_lists_valid_scopes():
    message = _capture_error(applicability_scope="global")
    assert "global" in message
    for scope in ("project_local", "workbench", "cross_project"):
        assert scope in message


def test_invalid_impact_lists_valid_impacts():
    message = _capture_error(impact="severe")
    assert "severe" in message
    for impact in ("low", "medium", "high", "critical"):
        assert impact in message


def test_cross_project_scope_states_the_required_cardinality():
    message = _capture_error(applicability_scope="cross_project", project_refs=["only-one"])
    assert "cross_project" in message
    assert "at least 2" in message
    assert "workbench" in message, "should point at the single-project alternative"


def test_project_local_scope_states_the_required_cardinality():
    message = _capture_error(applicability_scope="project_local", project_refs=[])
    assert "project_local" in message
    assert "exactly 1" in message


def test_unknown_capture_field_names_the_allowed_fields():
    message = _capture_error(interpretation="not a real field")
    assert "interpretation" in message
    assert "observation_type" in message and "signal_key" in message


def test_invalid_provenance_names_the_allowed_keys():
    message = _capture_error(provenance={"source": "somewhere"})
    assert "source" in message
    assert "source_action" in message and "source_version" in message


# ── #10: evidence errors list the reference types and show the form ──────────


def test_unknown_reference_type_lists_valid_types():
    from odibi_anchor.codebase.structured_learning_context import _evidence

    with pytest.raises(ValueError) as excinfo:
        _evidence([{"reference_type": "notebook", "reference": "abc"}])
    message = str(excinfo.value)
    assert "notebook" in message
    missing = [name for name in REFS if name not in message]
    assert not missing, f"types absent from error: {missing}"


def test_malformed_session_reference_shows_the_expected_form():
    """The reported case: a free-text session reference containing spaces."""
    from odibi_anchor.codebase.structured_learning_context import _evidence

    with pytest.raises(ValueError) as excinfo:
        _evidence([
            {
                "reference_type": "session",
                "reference": "2026-09-15 working session: garbled tags",
            },
        ])
    message = str(excinfo.value)
    assert "session" in message
    assert "summary" in message, "should say where the prose belongs"


def test_valid_evidence_still_accepted():
    """Negative control: the error-message changes must not reject good input."""
    from odibi_anchor.codebase.structured_learning_context import _evidence

    out = _evidence([
        {
            "reference_type": "session",
            "reference": "2026-09-15T17:55:00Z-asana-html-test",
            "summary": "html_text rendered garbled tags",
        },
    ])
    assert len(out) == 1


# ── #14: task mode errors enumerate the valid modes ──────────────────────────


def test_unsupported_task_mode_lists_valid_modes():
    with pytest.raises(ValueError) as excinfo:
        normalize_task_profile(legacy_mode="governance")
    message = str(excinfo.value)
    assert "governance" in message
    for mode in ("planning", "implementation", "documentation"):
        assert mode in message, f"{mode} missing from {message!r}"


def test_unsupported_task_mode_message_names_execution_mode():
    """The mode decides what may change, so the message says which each grants."""
    with pytest.raises(ValueError) as excinfo:
        normalize_task_profile(legacy_mode="governance")
    message = str(excinfo.value)
    assert "source_change" in message
    assert "artifact_only" in message


def test_every_legacy_mode_is_listed():
    with pytest.raises(ValueError) as excinfo:
        normalize_task_profile(legacy_mode="not-a-mode")
    message = str(excinfo.value)
    missing = [mode for mode in _LEGACY_DEFAULTS if mode not in message]
    assert not missing, f"modes absent from error message: {missing}"


# ── #11: capture returns the observation id at the top level ─────────────────


def test_capture_result_exposes_observation_id_at_top_level():
    result = _capture_result({"item": {"item_id": "lrn_abc123"}, "deduped": False})
    assert result["observation_id"] == "lrn_abc123"
    assert result["item"]["item_id"] == "lrn_abc123", "nested id must still be present"


# ── #9/#10: help documents the constraints that errors enforce ───────────────


def test_help_documents_every_reference_type():
    text = build_help_text("learning", None)
    missing = [name for name in REFS if f"`{name}`" not in text]
    assert not missing, f"reference types absent from help: {missing}"


def test_help_documents_project_refs_cardinality():
    text = build_help_text("learning", None)
    assert "project_refs" in text
    assert "cross_project" in text and "at least two" in text
    assert "exactly one" in text


def test_help_documents_provenance_keys():
    text = build_help_text("learning", None)
    assert "source_action" in text and "source_version" in text


def test_help_documents_capture_response_shape():
    text = build_help_text("learning", None)
    assert "observation_id" in text
    assert "deduped" in text


# ── #12: the memory-disposition prerequisite is documented ───────────────────


def test_help_documents_memory_disposition_prerequisite():
    text = build_help_text("learning", None)
    assert "disposition" in text
    assert "all_pending" in text


# ── #14 part two / prior drift: task help enumerates modes and scope shape ───


def test_task_help_enumerates_modes():
    text = build_help_text("task", None)
    missing = [mode for mode in _LEGACY_DEFAULTS if mode not in text]
    assert not missing, f"modes absent from task help: {missing}"


def test_task_help_describes_repository_scope_as_a_list():
    """`.assistant_instructions.md` called this a prose string; the runtime wants a list."""
    text = build_help_text("task", None)
    assert "repository_scope" in text


def test_assess_example_uses_real_observation_id_prefix():
    """Captured ids are `lrn_`; an `obs_` example sends callers looking for the wrong key."""
    text = build_help_text("learning", None)
    assert "obs_..." not in text
