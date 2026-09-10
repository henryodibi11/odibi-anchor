"""Shared fixtures for tests/utils/ — session state and utility tests.

Provides:
    clean_session_state: Auto-used fixture that isolates each test from
        session state pollution. Saves/restores the singleton's mutable
        objects so tests can mutate freely without side effects.
"""

import pytest


@pytest.fixture(autouse=True)
def clean_session_state():
    """Isolate tests from shared session state.

    Saves the current state of the _session_state singleton before each test,
    clears it for a clean slate, and restores the original state afterward.
    This ensures test ordering doesn't affect results.
    """
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED,
        _SESSION_FILES_CREATED,
        _SESSION_TIMINGS,
    )
    # Snapshot current state
    saved_changed = _SESSION_FILES_CHANGED.copy()
    saved_created = _SESSION_FILES_CREATED.copy()
    saved_timings = _SESSION_TIMINGS.copy()

    # Clear for test isolation
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CREATED.clear()
    _SESSION_TIMINGS.clear()

    yield

    # Restore original state
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CHANGED.update(saved_changed)
    _SESSION_FILES_CREATED.clear()
    _SESSION_FILES_CREATED.update(saved_created)
    _SESSION_TIMINGS.clear()
    _SESSION_TIMINGS.extend(saved_timings)
