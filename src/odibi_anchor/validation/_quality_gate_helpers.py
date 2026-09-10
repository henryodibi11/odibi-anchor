"""Shared helpers for quality gate check modules.

This module exists to avoid circular imports between
quality_gate_context.py and the split check modules.
"""

from __future__ import annotations

import math
from typing import Any


# Type category mappings for compatibility checks
_NUMERIC_TYPES = {
    "int", "integer", "long", "short", "byte",
    "float", "double", "decimal", "numeric",
    "int64", "int32", "int16", "int8",
    "float64", "float32", "bigint", "smallint",
    "tinyint", "number",
}
_STRING_TYPES = {
    "string", "str", "object", "varchar", "char",
    "text", "nvarchar",
}
_TEMPORAL_TYPES = {
    "timestamp", "date", "datetime",
    "datetime64", "datetime64[ns]",
    "timestamp_ntz", "timestamptype",
    "datetype",
}
_BOOLEAN_TYPES = {"boolean", "bool"}


def _check_id_label(check_id: str) -> str:
    """Map check_id to a human-readable label."""
    _LABELS = {
        "duplicate_keys": "Duplicate Keys",
        "null_keys": "Null Key Values",
        "schema_compat": "Schema Compatibility",
        "completeness": "Column Completeness",
        "row_count": "Row Count",
    }
    return _LABELS.get(check_id, check_id.replace("_", " ").title())


_CHECK_INTERPRETATIONS: dict[str, str] = {
    "duplicate_keys": "Ambiguous write grain — merging with duplicate keys will cause row multiplication.",
    "null_keys": "Merge/join will silently drop rows with null keys — every null key row is invisible to downstream consumers.",
    "schema_compat": "Upstream contract change — if column types or names shifted, existing transforms may break or silently produce wrong results.",
    "completeness": "Missing values in key columns — if completeness is low, aggregations and filters may under-count.",
    "row_count": "Row count outside expected range — could indicate a broken upstream extract or an incomplete load.",
}


def _type_category(type_str: str) -> str | None:
    """Categorize a type string into a group.

    Returns one of: 'numeric', 'string', 'temporal',
    'boolean', or None if unrecognizable.
    """
    t = type_str.lower().strip()
    # Remove pyspark suffixes like "()"
    t = t.rstrip("()")
    t = t.replace("type", "")
    t = t.strip()

    if any(n in t for n in _NUMERIC_TYPES):
        return "numeric"
    if any(s in t for s in _STRING_TYPES):
        return "string"
    if any(d in t for d in _TEMPORAL_TYPES):
        return "temporal"
    if any(b in t for b in _BOOLEAN_TYPES):
        return "boolean"
    return None


def _extract_target_columns(
    target_schema: Any,
) -> dict[str, str] | None:
    """Extract column name -> type mapping from schema.

    Supports Spark StructType, dict, and pandas dtypes.
    """
    if target_schema is None:
        return None

    if isinstance(target_schema, dict):
        return {
            str(k): str(v)
            for k, v in target_schema.items()
        }

    type_name = type(target_schema).__name__
    if type_name == "StructType":
        try:
            return {
                f.name: str(f.dataType)
                for f in target_schema.fields
            }
        except (AttributeError, TypeError):
            return None

    if hasattr(target_schema, "items"):
        try:
            return {
                str(k): str(v)
                for k, v in target_schema.items()
            }
        except (AttributeError, TypeError):
            return None

    return None


def _make_check(
    *,
    check_id: str,
    status: str,
    severity: str,
    detail: str,
    fix_expr: str,
    samples: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a standardized check result dict."""
    result: dict[str, Any] = {
        "check_id": check_id,
        "status": status,
        "severity": severity,
        "detail": detail,
        "fix_expr": fix_expr,
        "samples": samples,
    }
    if extra:
        result.update(extra)
    return result


def _format_age(hours: float) -> str:
    """Format age in hours to human-readable string."""
    if hours < 1:
        minutes = int(hours * 60)
        return f"{minutes}m"
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


def _pandas_rows_to_dicts(
    df: Any,
) -> list[dict[str, Any]]:
    """Convert pandas DataFrame rows to safe dicts."""
    records = df.to_dict(orient="records")
    return [_sanitize_dict(r) for r in records]


def _spark_rows_to_dicts(
    rows: list,
) -> list[dict[str, Any]]:
    """Convert Spark Row objects to safe dicts."""
    return [
        _sanitize_dict(r.asDict()) for r in rows
    ]


def _sanitize_dict(d: dict) -> dict[str, Any]:
    """Make a dict JSON-serializable."""
    clean: dict[str, Any] = {}
    for k, v in d.items():
        clean[str(k)] = _sanitize_value(v)
    return clean


def _sanitize_value(v: Any) -> Any:
    """Convert non-serializable values to safe types."""
    if v is None:
        return None
    if isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, (list, tuple)):
        return [_sanitize_value(x) for x in v]
    if isinstance(v, dict):
        return _sanitize_dict(v)
    return str(v)


def _check_row_count(
    total_rows: int,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Check row count sanity (engine-agnostic)."""
    min_rows = thresholds.get("min_rows", 10)

    if total_rows == 0:
        return _make_check(
            check_id="row_count",
            status="fail",
            severity="blocker",
            detail="DataFrame is empty (0 rows)",
            fix_expr=(
                "# Cannot fix empty DataFrame "
                "- check upstream source"
            ),
            samples=[],
        )
    if total_rows < min_rows:
        return _make_check(
            check_id="row_count",
            status="warn",
            severity="warning",
            detail=(
                f"Only {total_rows} rows "
                f"(threshold: {min_rows})"
            ),
            fix_expr="",
            samples=[],
        )
    return _make_check(
        check_id="row_count",
        status="pass",
        severity="blocker",
        detail=f"{total_rows:,} rows",
        fix_expr="",
        samples=[],
    )


def _build_context(
    *,
    check_results: list[dict[str, Any]],
    total_rows: int,
    subject: str,
    engine_name: str,
    df_name: str,
) -> dict[str, Any]:
    """Assemble the final context dictionary."""
    checks_passed = sum(
        1 for c in check_results
        if c["status"] == "pass"
    )
    checks_failed = sum(
        1 for c in check_results
        if c["status"] == "fail"
    )
    checks_warned = sum(
        1 for c in check_results
        if c["status"] == "warn"
    )

    is_write_safe = checks_failed == 0

    if checks_failed > 0:
        status = "fail"
    elif checks_warned > 0:
        status = "warn"
    else:
        status = "pass"

    # Summary – verdict-first with human-readable labels
    total_checks = len(check_results)
    if checks_failed > 0:
        blocked_labels = [
            _check_id_label(c["check_id"])
            for c in check_results
            if c["status"] == "fail"
        ]
        summary = (
            f"Write blocked — {', '.join(blocked_labels).lower()} "
            f"need resolution before merge. "
            f"({checks_passed}/{total_checks} checks passed)"
        )
    elif checks_warned > 0:
        warned_labels = [
            _check_id_label(c["check_id"])
            for c in check_results
            if c["status"] == "warn"
        ]
        summary = (
            f"Write safe with warnings — review "
            f"{', '.join(warned_labels).lower()} before proceeding. "
            f"({checks_passed}/{total_checks} checks passed)"
        )
    else:
        summary = (
            f"Write safe — all {total_checks} checks passed."
        )

    # Build fix_all_expr
    fix_all_expr = _build_fix_all_expr(
        check_results, df_name
    )

    # Fix impact
    fix_impact = _build_fix_impact(
        check_results, total_rows
    )

    # Findings and risks
    findings = _build_findings(check_results)
    risks = _build_risks(check_results)

    # Recommendation
    recommendation = _build_recommendation(
        checks_failed, checks_warned,
        check_results, fix_impact,
    )

    # Suggested next actions
    actions = _build_actions(
        check_results, is_write_safe, fix_impact
    )
    # ── Graph wiring (audit fix) ──
    actions.append("If PASSED: Run anchor(\"diff\", old_df, new_df, keys=[...]) to compare with previous.")
    actions.append("If PASSED: Run anchor(\"validate\", df, rules=[...]) for semantic business rules.")
    actions.append("If FAILED: Run anchor(\"duplicate\", df, keys) to diagnose key violations.")
    actions.append("If FAILED: Run anchor(\"explore\", df) to understand data shape.")
    actions.append("MUST: Run anchor(\"touched\") + anchor(\"gate\") after code changes.")

    return {
        "kind": "quality_gate_context",
        "subject": subject,
        "engine": engine_name,
        "status": status,
        "summary": summary,
        "metrics": {
            "total_rows": total_rows,
            "checks_run": total_checks,
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "checks_warned": checks_warned,
            "is_write_safe": is_write_safe,
        },
        "checks": check_results,
        "findings": findings,
        "risks": risks,
        "samples": {
            c["check_id"]: c["samples"]
            for c in check_results
            if c.get("samples")
        },
        "fix_all_expr": fix_all_expr,
        "fix_impact": fix_impact,
        "recommendation": recommendation,
        "suggested_next_actions": actions,
    }


def _build_fix_all_expr(
    check_results: list[dict[str, Any]],
    df_name: str,
) -> str:
    """Combine fix expressions for failed checks."""
    fix_lines: list[str] = []
    for c in check_results:
        if c["status"] == "fail" and c["fix_expr"]:
            fix_lines.append(
                f"# Fix: {c['check_id']}"
            )
            fix_lines.append(c["fix_expr"])
    if not fix_lines:
        return ""
    header = "# Run these in order:"
    return header + "\n" + "\n".join(fix_lines)


def _build_fix_impact(
    check_results: list[dict[str, Any]],
    total_rows: int,
) -> dict[str, Any]:
    """Estimate data loss from applying all fixes.

    Computes an upper-bound estimate. Actual loss may
    be lower because some rows may have both null keys
    AND be duplicates.
    """
    total_dropped = 0
    for c in check_results:
        dropped = c.get("rows_dropped_by_fix", 0)
        total_dropped += dropped

    # Cap at total_rows (overlap means real drop
    # is less)
    total_dropped = min(total_dropped, total_rows)
    est_after = total_rows - total_dropped
    drop_pct = (
        round(total_dropped / total_rows * 100, 1)
        if total_rows > 0
        else 0.0
    )

    impact: dict[str, Any] = {
        "rows_before": total_rows,
        "estimated_rows_after": est_after,
        "estimated_rows_dropped": total_dropped,
        "drop_pct": drop_pct,
        "is_upper_bound": True,
    }
    if drop_pct > 5:
        impact["upstream_concern"] = (
            f"Fixing would drop >{drop_pct:.0f}% of data — this suggests an upstream defect rather than a local data quality issue. "
            f"Investigate the source before applying fixes."
        )
    return impact


def _build_findings(
    check_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build findings list from check results."""
    findings: list[dict[str, Any]] = []
    for c in check_results:
        if c["status"] in ("fail", "warn"):
            findings.append({
                "check_id": c["check_id"],
                "severity": c["severity"],
                "detail": c["detail"],
                "interpretation": _CHECK_INTERPRETATIONS.get(c["check_id"], ""),
            })
    return findings


def _build_risks(
    check_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build risks list from check results."""
    risks: list[dict[str, Any]] = []
    for c in check_results:
        if c["status"] == "fail":
            interp = _CHECK_INTERPRETATIONS.get(c["check_id"], "")
            message = (
                f"{_check_id_label(c['check_id'])} failed: "
                f"{c['detail']}"
            )
            if interp:
                message += f" — {interp}"
            risks.append({
                "type": c["check_id"],
                "severity": "high",
                "message": message,
            })
        elif c["status"] == "warn":
            interp = _CHECK_INTERPRETATIONS.get(c["check_id"], "")
            message = (
                f"{_check_id_label(c['check_id'])}: "
                f"{c['detail']}"
            )
            if interp:
                message += f" — {interp}"
            risks.append({
                "type": c["check_id"],
                "severity": "medium",
                "message": message,
            })
    return risks


def _build_recommendation(
    checks_failed: int,
    checks_warned: int,
    check_results: list[dict[str, Any]],
    fix_impact: dict[str, Any],
) -> str:
    """Build a recommendation string."""
    if checks_failed > 0:
        blocked_labels = [
            _check_id_label(c["check_id"])
            for c in check_results
            if c["status"] == "fail"
        ]
        drop_pct = fix_impact["drop_pct"]
        base = (
            f"BLOCKED. Resolve {', '.join(blocked_labels).lower()} "
            f"before writing."
        )
        if drop_pct > 0:
            base += (
                f" Fixing likely drops ~{drop_pct}% "
                f"of rows."
            )
        return base
    if checks_warned > 0:
        warned_labels = [
            _check_id_label(c["check_id"])
            for c in check_results
            if c["status"] == "warn"
        ]
        return (
            f"SAFE with warnings — review "
            f"{', '.join(warned_labels).lower()} before writing."
        )
    return "SAFE. All checks passed."


def _build_actions(
    check_results: list[dict[str, Any]],
    is_write_safe: bool,
    fix_impact: dict[str, Any],
) -> list[str]:
    """Build suggested next actions list with priority prefixes and anchor() hints."""
    actions: list[str] = []
    if not is_write_safe:
        drop_pct = fix_impact["drop_pct"]
        if drop_pct > 20:
            actions.append(
                f"MUST: Review before applying — fixes drop ~{drop_pct}% of rows"
            )
        actions.append(
            "MUST: Apply fix_all_expr to resolve blockers, then re-run "
            "anchor('quality', df, subject=..., keys=[...]) to verify"
        )
        # Check-specific diagnostic suggestions
        for c in check_results:
            if c["status"] != "fail":
                continue
            label = _check_id_label(c["check_id"])
            if c["check_id"] == "duplicate_keys":
                keys = c.get("keys", [])
                if keys:
                    actions.append(
                        f"Run anchor('duplicate', df, keys={keys}) to diagnose {label.lower()} violations."
                    )
                else:
                    actions.append(
                        f"Run anchor('duplicate', df, keys=[...]) to diagnose {label.lower()} violations."
                    )
            elif c["check_id"] == "null_keys":
                keys = c.get("keys", [])
                if keys:
                    actions.append(
                        f"Run anchor('explore', df[{keys}]) to understand null patterns in {label.lower()}."
                    )
                else:
                    actions.append(
                        f"Run anchor('explore', df[key_cols]) to understand null patterns in {label.lower()}."
                    )
            elif c["check_id"] == "schema_compat":
                actions.append(
                    f"Run anchor('schema_diff', old_schema, new_schema) to see exact {label.lower()} changes."
                )
    else:
        actions.append("MUST: Proceed with write/save — all quality checks passed")
    has_warnings = any(
        c["status"] == "warn"
        for c in check_results
    )
    if has_warnings:
        warned_labels = [
            _check_id_label(c["check_id"])
            for c in check_results
            if c["status"] == "warn"
        ]
        actions.append(
            f"MUST: Review {', '.join(warned_labels).lower()} warnings — consider "
            "anchor('profile_table', df, subject='...') to inspect flagged columns"
        )
    return actions
