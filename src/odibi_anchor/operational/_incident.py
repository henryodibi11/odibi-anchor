"""Fail-forward incident evidence composition, independent of dispatch wiring."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ._artifacts import persist_artifact
from ._contract import CollectorResult, canonical_json, normalize_json, utc_now
from ._environment import capture_environment
from ._redaction import redact

_SCALAR_TYPES = (str, int, float, bool, type(None))


def _scalar_fields(value: Any, names: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {name: value[name] for name in names if name in value and type(value[name]) in _SCALAR_TYPES}


def _string_list(value: Any) -> list[str]:
    return list(value[:100]) if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value) else []


def _mapping_summary(value: Any) -> dict[str, Any] | None:
    """Represent an arbitrary normalized mapping without retaining its values."""
    if not isinstance(value, Mapping):
        return None
    return {
        "member_count": len(value),
        "sha256": hashlib.sha256(canonical_json(value)).hexdigest(),
    }


def _project_local(facts: Mapping[str, Any]) -> dict[str, Any]:
    projected = _scalar_fields(facts, ("odibi_anchor_version", "execution_mode", "identity"))
    projected["runtime"] = _scalar_fields(facts.get("runtime"), ("implementation", "python_version"))
    projected["platform"] = _scalar_fields(facts.get("platform"), ("system", "machine"))
    packages = facts.get("packages")
    projected["packages"] = {
        str(name): _scalar_fields(value, ("available", "version"))
        for name, value in packages.items()
    } if isinstance(packages, Mapping) else {}
    roots = facts.get("roots")
    projected["roots"] = {
        str(name): value for name, value in roots.items() if isinstance(value, str)
    } if isinstance(roots, Mapping) else {}
    projected["actions"] = _string_list(facts.get("actions"))
    projected["environment_variable_names"] = _string_list(facts.get("environment_variable_names"))
    return projected


def _project_spark(facts: Mapping[str, Any]) -> dict[str, Any]:
    projected = _scalar_fields(facts, ("plan_sha256", "plan_line_count"))
    projected["metrics"] = _scalar_fields(
        facts.get("metrics"), ("record_count", "spill_bytes", "max_partition_bytes", "median_partition_bytes"),
    )
    return projected


def _project_uc(facts: Mapping[str, Any]) -> dict[str, Any]:
    detail = facts.get("detail")
    projected_detail = _scalar_fields(
        detail,
        ("table_name", "catalog_name", "schema_name", "type", "provider", "location", "comment", "format"),
    )
    if isinstance(detail, Mapping):
        properties = detail.get("table_properties", detail.get("properties"))
        projected_properties = _scalar_fields(
            properties, ("delta.enableChangeDataFeed", "delta.logRetentionDuration", "delta.deletedFileRetentionDuration"),
        )
        if projected_properties:
            projected_detail["properties"] = projected_properties
        columns = []
        for column in detail.get("columns", ()) if isinstance(detail.get("columns"), (list, tuple)) else ():
            item = _scalar_fields(column, ("name", "type", "type_name", "comment", "nullable", "partition_index"))
            if item:
                columns.append(item)
        projected_detail["columns"] = columns[:500]
    grants = []
    for grant in facts.get("direct_grants", ()) if isinstance(facts.get("direct_grants"), (list, tuple)) else ():
        item = _scalar_fields(
            grant, ("principal", "action_type", "privilege", "object_type", "object_key", "inherited_from"),
        )
        if item:
            grants.append(item)
    return {
        "object": _scalar_fields(facts.get("object"), ("name", "type")),
        "detail": projected_detail,
        "direct_grants": grants[:500],
        "tags": None,
        "dependencies": None,
        "table_lineage": None,
        "column_lineage": None,
        "row_filters": None,
        "column_masks": None,
        "execution_principal": None,
    }


def _project_delta(facts: Mapping[str, Any]) -> dict[str, Any]:
    cdf = _scalar_fields(facts.get("cdf"), ("enabled", "readable_from", "readable_to", "expired"))
    if isinstance(facts.get("cdf"), Mapping):
        cdf["change_counts"] = _scalar_fields(
            facts["cdf"].get("change_counts"), ("insert", "update_preimage", "update_postimage", "delete"),
        )
        cdf["affected_columns"] = _string_list(facts["cdf"].get("affected_columns"))
        cdf["hashed_changed_keys"] = _string_list(facts["cdf"].get("hashed_changed_keys"))
    history = []
    for entry in facts.get("history", ()) if isinstance(facts.get("history"), (list, tuple)) else ():
        item = _scalar_fields(entry, ("version", "timestamp", "operation"))
        metrics = _mapping_summary(entry.get("operation_metrics")) if isinstance(entry, Mapping) else None
        if metrics:
            item["operation_metrics"] = metrics
        if item:
            history.append(item)
    return {
        **_scalar_fields(facts, ("current_version", "format", "table_type", "retention")),
        "cdf": cdf,
        "history": history[:100],
        "restore_prerequisites": _scalar_fields(
            facts.get("restore_prerequisites"),
            ("target_version_available", "files_available", "permissions_verified"),
        ),
    }


def _project_run_diff(facts: Mapping[str, Any]) -> dict[str, Any]:
    comparisons = []
    for item in facts.get("comparisons", ()) if isinstance(facts.get("comparisons"), (list, tuple)) else ():
        projected = _scalar_fields(item, ("field", "label", "changed", "evidence"))
        if projected:
            comparisons.append(projected)
    return {"comparisons": comparisons[:100]}


def _project_run(facts: Mapping[str, Any]) -> dict[str, Any]:
    run = facts.get("run")
    projected = _scalar_fields(run, ("run_id", "status", "code_revision", "runtime", "target_schema", "duration"))
    if isinstance(run, Mapping):
        for name in ("parameters", "packages", "inputs", "watermarks", "failure_metadata", "identity"):
            summary = _mapping_summary(run.get(name))
            if summary:
                projected[f"{name}_summary"] = summary
    return {"run": projected}


def _project_databricks_environment(facts: Mapping[str, Any]) -> dict[str, Any]:
    projected = _scalar_fields(facts, ("runtime", "execution_mode", "identity", "workspace_id"))
    summary = _mapping_summary(facts.get("packages"))
    if summary:
        projected["packages_summary"] = summary
    return projected


_FACT_PROJECTORS = {
    "local_environment": _project_local,
    "spark_diagnose": _project_spark,
    "uc_context": _project_uc,
    "delta_changes": _project_delta,
    "run_diff": _project_run_diff,
    "run_context": _project_run,
    "databricks_environment": _project_databricks_environment,
}


def _failure(name: str, exc: Exception) -> CollectorResult:
    if isinstance(exc, PermissionError):
        status = "denied"
    elif isinstance(exc, (ImportError, ModuleNotFoundError, NotImplementedError)):
        status = "unavailable"
    else:
        status = "failed"
    return CollectorResult(name, status, error={"type": type(exc).__name__, "message": str(exc)[:1000]},
                           limitations=(f"{name} evidence was not collected",))


def _collect(item: Any, index: int) -> CollectorResult:
    try:
        value = item() if callable(item) else item
        if isinstance(value, CollectorResult):
            return value
        if isinstance(value, Mapping):
            return CollectorResult(**value)
        raise TypeError("collector must return CollectorResult or a mapping")
    except Exception as exc:  # collectors are intentionally isolated
        return _failure(getattr(item, "__name__", f"collector_{index}"), exc)


def _persistable_result(result: CollectorResult) -> dict[str, Any]:
    """Project only registered collector schemas into durable incident evidence."""
    value = result.to_dict()
    projector = _FACT_PROJECTORS.get(result.collector)
    if projector is None:
        return normalize_json({
            "collector": result.collector,
            "status": result.status,
            "facts": {},
            "findings": [],
            "limitations": [*result.limitations, "unregistered collector facts were omitted from persistence"],
            "observed_at": result.observed_at,
            "schema_version": result.schema_version,
        })
    findings = []
    for finding in value["findings"]:
        item = _scalar_fields(finding, ("kind", "statement"))
        if isinstance(finding, Mapping):
            item["evidence"] = _scalar_fields(
                finding.get("evidence"),
                ("channel", "line", "node", "record", "spill_bytes", "max_to_median_ratio",
                 "current_version", "history_entries", "count", "field", "changed", "basis"),
            )
        if item:
            findings.append(item)
    return normalize_json({
        "collector": result.collector,
        "status": result.status,
        "source": _scalar_fields(value["source"], ("provider", "channel")),
        "environment": _scalar_fields(value["environment"], ("platform", "execution_mode", "evidence_channel")),
        "facts": projector(value["facts"]),
        "findings": findings[:100],
        "limitations": [item for item in value["limitations"] if isinstance(item, str)][:100],
        "error": _scalar_fields(value.get("error"), ("type", "message")) or None,
        "redaction": _scalar_fields(value["redaction"], ("count",)),
        "observed_at": result.observed_at,
        "schema_version": result.schema_version,
    })


def incident_snapshot(collectors: Sequence[Any] = (), *, artifact_root: str | Path,
                      problem_id: str | None = None, attach: Callable[[str, Mapping[str, Any]], Any] | None = None,
                      environment_options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Collect independently, redact, persist once, then optionally attach provenance."""
    results = [capture_environment(**dict(environment_options or {})),
               *[_collect(item, index) for index, item in enumerate(collectors)]]
    captured_at = utc_now()
    incident_id = f"INC-{captured_at.replace(':', '').replace('-', '')}-{secrets.token_hex(6)}"
    persisted_results = [_persistable_result(result) for result in results]
    established = []
    limitations = []
    for result in persisted_results:
        if result["status"] == "collected" and result["facts"]:
            established.append({"collector": result["collector"], "facts": result["facts"]})
        limitations.extend(result["limitations"])
        if result["status"] != "collected":
            limitations.append(f"{result['collector']}: {result['status']}")
    payload: dict[str, Any] = {
        "schema_version": 1, "kind": "incident_snapshot", "incident_id": incident_id,
        "captured_at": captured_at, "collectors": persisted_results,
        "synthesis": {"established_facts": established, "hypotheses": [], "conflicting_evidence": [],
                      "material_uncertainty": bool(limitations), "limitations": sorted(set(limitations))},
        "collection_policy": {"fail_forward": True, "local_environment": True, "registered_schemas_only": True},
    }
    cleaned, summary = redact(payload)
    cleaned["redaction"] = summary
    cleaned["integrity"] = {"algorithm": "sha256", "digest": hashlib.sha256(canonical_json(cleaned)).hexdigest()}
    evidence = normalize_json(cleaned)
    artifact = persist_artifact(evidence, artifact_root, kind="incidents")
    managed_writes = [{
        "kind": "incident_snapshot", "status": "written", "path": str(artifact.path),
        "relative_path": artifact.relative_path, "sha256": artifact.sha256,
    }]
    attachment = {"requested": problem_id is not None, "status": "not_requested"}
    if problem_id is not None:
        row = {
            "source": "operational_evidence",
            "captured": captured_at,
            "observation": f"Incident {incident_id} captured at {artifact.relative_path} (sha256:{artifact.sha256}).",
            "interpretation": "See the immutable incident artifact for established facts, failed channels, and limitations.",
            "confidence": "high",
        }
        if attach is None:
            attachment = {"requested": True, "status": "failed", "error": "attachment callback not supplied"}
        else:
            try:
                receipt = attach(problem_id, normalize_json(row))
                if not isinstance(receipt, Mapping) or receipt.get("write_performed") is not True:
                    raise RuntimeError("attachment callback did not return a successful write receipt")
                if not receipt.get("accepted_evidence"):
                    raise RuntimeError("attachment callback did not confirm accepted evidence")
                attachment_writes = receipt.get("managed_writes") or [{
                    "kind": "problem", "status": "written", "path": receipt.get("artifact_path"),
                }]
                managed_writes.extend(write for write in attachment_writes if write.get("path"))
                attachment = {"requested": True, "status": "attached", "problem_id": problem_id}
            except Exception as exc:
                attachment = {"requested": True, "status": "failed",
                              "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    return normalize_json({
        **evidence,
        "evidence": evidence,
        "managed_writes": managed_writes,
        "write_performed": True,
        "attachment": attachment,
    })


def render_incident_markdown(value: Mapping[str, Any]) -> str:
    lines = [f"# Incident {value['incident_id']}", "", f"Captured: {value['captured_at']}", "", "## Evidence"]
    lines.extend(f"- {item['collector']}: **{item['status']}**" for item in value.get("collectors", ()))
    lines.extend(["", "## Limitations"])
    lines.extend(f"- {item}" for item in value.get("synthesis", {}).get("limitations", ()))
    return "\n".join(lines) + "\n"
