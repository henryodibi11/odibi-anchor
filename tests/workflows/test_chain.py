"""Tests for anchor("chain") cross-workflow chaining action.

Covers:
- Error handling for missing result/target
- investigate → evolve chain (coerce_check extraction)
- onboard → investigate chain (profile extraction)
- reconcile → debug chain (diff keys extraction)
- debug → trace_row chain (explain_row extraction)
- Unsupported target returns helpful error
- No chainable data returns informative message
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from odibi_anchor._dispatcher._tool_wrappers import _chain_context


class TestChainErrors:
    def test_no_result(self):
        result = _chain_context(target="evolve", output_format="dict")
        assert result["kind"] == "chain_context"
        assert result["subject"] == "error"
        assert "No workflow result" in result["summary"]

    def test_no_target(self):
        result = _chain_context(result={"kind": "workflow_investigate", "steps": []}, output_format="dict")
        assert result["subject"] == "error"
        assert "No target" in result["summary"]

    def test_unsupported_target(self):
        result = _chain_context(
            result={"kind": "workflow_investigate", "steps": []},
            target="nonexistent",
            output_format="dict",
        )
        assert result["subject"] == "error"
        assert "Unsupported target" in result["summary"]
        assert "nonexistent" in result["summary"]


class TestChainInvestigateToEvolve:
    def test_extracts_coerce_ctx(self):
        coerce_result = {
            "kind": "coercion_check_context",
            "metrics": {"columns_with_issues": 2},
            "samples": {"column_results": {"amount": {}}},
        }
        investigate_output = {
            "kind": "workflow_investigate",
            "steps": [
                {"tool": "profile_table", "status": "ok", "result": {}},
                {"tool": "coerce_check", "status": "ok", "result": coerce_result},
            ],
        }
        result = _chain_context(investigate_output, target="evolve", output_format="dict")
        assert result["kind"] == "chain_context"
        assert "coerce_ctx" in result["samples"]["kwargs"]
        assert result["samples"]["kwargs"]["coerce_ctx"] == coerce_result

    def test_no_coerce_check_step(self):
        investigate_output = {
            "kind": "workflow_investigate",
            "steps": [
                {"tool": "profile_table", "status": "ok", "result": {}},
                {"tool": "coerce_check", "status": "skipped", "result": None},
            ],
        }
        result = _chain_context(investigate_output, target="evolve", output_format="dict")
        assert "No chainable context" in result["summary"]


class TestChainOnboardToInvestigate:
    def test_extracts_high_null_columns(self):
        onboard_output = {
            "kind": "workflow_onboard",
            "subject": "orders",
            "steps": [
                {
                    "tool": "profile_table",
                    "status": "ok",
                    "result": {
                        "samples": {
                            "column_profiles": {
                                "id": {"null_rate": 0.0},
                                "amount": {"null_rate": 0.3},
                                "notes": {"null_rate": 0.8},
                            }
                        }
                    },
                },
            ],
        }
        result = _chain_context(onboard_output, target="investigate", output_format="dict")
        assert result["kind"] == "chain_context"
        kwargs = result["samples"]["kwargs"]
        assert "amount" in kwargs["columns"]
        assert "notes" in kwargs["columns"]
        assert "id" not in kwargs.get("columns", [])


class TestChainReconcileToDebug:
    def test_extracts_keys(self):
        reconcile_output = {
            "kind": "workflow_reconcile",
            "steps": [
                {
                    "tool": "diff",
                    "status": "ok",
                    "result": {"metrics": {"keys": ["order_id"]}},
                },
            ],
        }
        result = _chain_context(reconcile_output, target="debug", output_format="dict")
        assert result["kind"] == "chain_context"
        assert result["samples"]["kwargs"]["keys"] == ["order_id"]


class TestChainDebugToTraceRow:
    def test_extracts_problematic_upstreams(self):
        debug_output = {
            "kind": "workflow_debug",
            "steps": [
                {
                    "tool": "explain_row",
                    "status": "ok",
                    "result": {
                        "metrics": {"keys": ["customer_id"]},
                        "samples": {
                            "join_paths": [
                                {"upstream": "orders", "match_count": 1},
                                {"upstream": "customers", "match_count": 0},
                            ]
                        },
                    },
                },
            ],
        }
        result = _chain_context(debug_output, target="trace_row", output_format="dict")
        assert result["kind"] == "chain_context"
        kwargs = result["samples"]["kwargs"]
        assert "customers" in kwargs["problematic_upstreams"]
        assert "orders" not in kwargs["problematic_upstreams"]


class TestChainPositionalArgs:
    def test_positional_result_and_target(self):
        investigate_output = {
            "kind": "workflow_investigate",
            "steps": [
                {"tool": "coerce_check", "status": "ok", "result": {"kind": "coercion_check"}},
            ],
        }
        result = _chain_context(investigate_output, "evolve", output_format="dict")
        assert result["kind"] == "chain_context"
        assert "coerce_ctx" in result["samples"]["kwargs"]
