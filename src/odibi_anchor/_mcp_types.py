"""Argument types for the typed MCP tools in ``mcp_server``.

This module deliberately has no ``from __future__ import annotations``. Pydantic builds a
tool's call validator on its first call and must resolve these TypedDicts' annotations;
string annotations resolve through ``sys.modules``, but bootstrap purges every
``odibi_anchor`` module from it, so string annotations failed to resolve after boot.
Evaluated annotations need no lookup.
"""
from typing import Literal, NotRequired

from pydantic import ConfigDict, with_config
from typing_extensions import TypedDict  # pydantic requires it before Python 3.12

_Forbid = ConfigDict(extra="forbid")


@with_config(_Forbid)
class PlanCriterion(TypedDict):
    id: str
    expected: str
    method: Literal["pytest", "artifact_sha256"]
    test_targets: NotRequired[list[str]]
    expected_xfailed: NotRequired[int]
    expected_sha256: NotRequired[dict[str, str]]


@with_config(_Forbid)
class PlanDestination(TypedDict):
    """Required keys by kind: managed_artifacts none; github_ref repository+ref;
    github_release repository+tag; pypi_release name+version;
    databricks_workspace_files host."""

    kind: Literal[
        "managed_artifacts", "github_ref", "github_release", "pypi_release",
        "databricks_workspace_files",
    ]
    repository: NotRequired[str]
    ref: NotRequired[str]
    tag: NotRequired[str]
    name: NotRequired[str]
    version: NotRequired[str]
    host: NotRequired[str]


@with_config(_Forbid)
class PlanRequirement(TypedDict):
    id: str
    method: Literal["qualification_criterion", "producer_learning"]
    criterion_id: NotRequired[str]


@with_config(_Forbid)
class PlanReconciliation(TypedDict):
    requirements: list[PlanRequirement]
    reason: NotRequired[str]


@with_config(_Forbid)
class PlanRequiredChild(TypedDict):
    workflow_id: str
    plan_sha256: str


@with_config(_Forbid)
class WorkflowPlan(TypedDict):
    schema_version: Literal[1]
    goal: str
    risk: Literal["low", "medium", "high"]
    execution_mode: Literal["read_only", "artifact_only", "source_change", "data_change"]
    scope: NotRequired[list[str]]
    exclusions: NotRequired[list[str]]
    constraints: NotRequired[list[str]]
    risks: NotRequired[list[str]]
    stop_conditions: NotRequired[list[str]]
    unresolved_decisions: NotRequired[list[str]]
    source_paths: NotRequired[list[str]]
    artifact_paths: NotRequired[list[str]]
    criteria: NotRequired[list[PlanCriterion]]
    destination: NotRequired[PlanDestination]
    reconciliation: NotRequired[PlanReconciliation]
    required_children: NotRequired[list[PlanRequiredChild]]


@with_config(_Forbid)
class ReviewFinding(TypedDict):
    summary: str
    status: Literal["open", "resolved"]


@with_config(_Forbid)
class LearningEvidence(TypedDict):
    reference_type: Literal["file", "git_commit", "problem", "spec", "test", "session"]
    reference: str
    summary: NotRequired[str]
    observed_at: NotRequired[str]


@with_config(_Forbid)
class LearningProvenance(TypedDict):
    source_action: NotRequired[str]
    source_version: NotRequired[str]
