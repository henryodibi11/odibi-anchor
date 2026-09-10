"""Tests for session notebook auto-population."""

import json
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._memory import (
    _append_notebook_cells,
    append_gate_to_notebook,
    append_learn_to_notebook,
    _md_cell,
)


@pytest.fixture
def notebook(tmp_path):
    """Create a minimal session notebook."""
    nb_path = tmp_path / "test_session.ipynb"
    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Test Session\n"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    nb_path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return str(nb_path)


class TestAppendNotebookCells:
    def test_appends_cells(self, notebook):
        cell = _md_cell(["## New Section\n"])
        assert _append_notebook_cells(notebook, [cell]) is True

        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 2
        assert "New Section" in nb["cells"][1]["source"][0]

    def test_returns_false_on_missing_file(self):
        assert _append_notebook_cells("/nonexistent/path.ipynb", []) is False


class TestAppendGate:
    def test_appends_gate_results(self, notebook):
        gate_result = {
            "metrics": {"score": 8, "max_score": 10, "rating": "good", "all_verified": True},
            "findings": ["All tests passed", "No lint errors"],
        }
        assert append_gate_to_notebook(notebook, gate_result, {"a.py", "b.py"}, feature_num=1)

        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        # Original cell + 3 gate cells (header, files, score)
        assert len(nb["cells"]) == 4
        all_text = "".join(
            "".join(c["source"]) for c in nb["cells"]
        )
        assert "Feature 1" in all_text
        assert "PASSED" in all_text
        assert "a.py" in all_text

    def test_returns_false_without_path(self):
        assert append_gate_to_notebook("", {}, set()) is False
        assert append_gate_to_notebook(None, {}, set()) is False


class TestAppendLearn:
    def test_appends_learn_results(self, notebook):
        learn_result = {"metrics": {"memories_added": 3}}
        events = [
            {"type": "discovery", "detail": "Found a new pattern"},
            {"type": "decision", "detail": "Chose approach X"},
        ]
        assert append_learn_to_notebook(notebook, learn_result, session_events=events)

        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        assert len(nb["cells"]) == 2
        text = "".join(nb["cells"][1]["source"])
        assert "discovery" in text
        assert "Found a new pattern" in text
        assert "3 memory entries" in text

    def test_returns_false_without_path(self):
        assert append_learn_to_notebook("", {}) is False
