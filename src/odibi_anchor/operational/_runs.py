"""Conservative comparison of normalized run or incident-like mappings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._contract import CollectorResult, ContractError
from ._redaction import redact

_FIELDS = ("code_revision", "parameters", "runtime", "packages", "inputs", "watermarks",
           "target_schema", "duration", "failure_metadata", "identity", "status")
_RELEVANT = {"code_revision", "parameters", "runtime", "packages", "inputs", "watermarks", "target_schema", "identity"}


def _facts(value: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = value.get("facts", value)
    if not isinstance(candidate, Mapping):
        raise ContractError("run facts must be a mapping")
    run = candidate.get("run", candidate)
    return run if isinstance(run, Mapping) else candidate


def run_diff(left: Mapping[str, Any], right: Mapping[str, Any]) -> CollectorResult:
    """Compare runs without treating success/failure correlation as causation."""
    a, b = _facts(left), _facts(right)
    explicit = b.get("causal_evidence", {})
    explicit = explicit if isinstance(explicit, Mapping) else {}
    comparisons = []
    for field in _FIELDS:
        if field not in a or field not in b:
            comparisons.append({"field": field, "label": "unable to verify", "changed": None,
                                "evidence": "field missing from one or both runs"})
            continue
        changed = a[field] != b[field]
        if field == "status":
            label = "probably unrelated"
            reason = "outcome difference alone is not causal evidence"
        elif not changed:
            label = "probably unrelated"
            reason = "normalized values are equal"
        elif explicit.get(field):
            label = "likely causal"
            reason = "caller supplied explicit field-linked causal evidence"
        elif field in _RELEVANT:
            label = "potentially relevant"
            reason = "material input or execution-context difference; causality is unverified"
        else:
            label = "potentially relevant"
            reason = "observed difference; causality is unverified"
        comparisons.append({"field": field, "label": label, "changed": changed, "evidence": reason})
    cleaned, summary = redact({"comparisons": comparisons})
    findings = tuple({"kind": "run_difference", "statement": f"{item['field']}: {item['label']}",
                      "evidence": {"field": item["field"], "changed": item["changed"], "basis": item["evidence"]}}
                     for item in cleaned["comparisons"] if item["changed"] is True)
    return CollectorResult(collector="run_diff", status="collected",
                           source={"provider": "portable", "channel": "normalized_mappings"},
                           facts={"comparisons": cleaned["comparisons"]}, findings=findings,
                           limitations=("labels express evidence strength, not proof of causality",),
                           redaction=summary)
