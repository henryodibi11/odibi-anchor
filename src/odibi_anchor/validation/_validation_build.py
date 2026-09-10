"""Context builders for validation summary.

Internal module — not part of the public API.
"""

from __future__ import annotations

from typing import Any

_CRITICAL_FAILURE_RATE = 0.5  # Failed rate above which a rule is critical

# Rule types that represent structural constraints (block writes/merges)
_STRUCTURAL_RULE_TYPES = {"not_null", "unique", "primary_key", "foreign_key"}

# Rule types that represent domain/business constraints (warn, don't block)
_DOMAIN_RULE_TYPES = {"accepted_values", "range", "regex", "custom", "custom_sql"}


def _build_context(
    *,
    rule_results: list[dict[str, Any]],
    all_pass_masks: list[Any] | None,
    total_rows: int,
    engine_name: str,
    subject: str,
    df: Any = None,
    rows_with_failure_override: int | None = None,
    schema_findings: list[dict[str, Any]] | None = None,
    quarantine_call: str = "",
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the final context dict from rule results.

    Args:
        rule_results: Per-rule evaluation results.
        all_pass_masks: Pandas masks (None for Spark).
        total_rows: Total row count.
        engine_name: "pandas" or "spark".
        subject: Human label.
        df: Pandas DataFrame (for mask combination).
        rows_with_failure_override: Pre-computed (Spark).
        schema_findings: Rule-schema mismatch findings.
        quarantine_call: Generated quarantine code.
        rules: Original rule list.

    Returns:
        Complete validation context dictionary.
    """
    rules_passed = sum(
        1 for r in rule_results if r["passed"]
    )
    rules_failed = len(rule_results) - rules_passed
    blocker_failures = [
        r for r in rule_results
        if not r["passed"] and r["severity"] == "blocker"
    ]
    warning_failures = [
        r for r in rule_results
        if not r["passed"] and r["severity"] == "warning"
    ]

    # Rows with any failure
    if rows_with_failure_override is not None:
        rows_with_failure = rows_with_failure_override
    elif all_pass_masks and df is not None:
        combined = all_pass_masks[0]
        for m in all_pass_masks[1:]:
            combined = combined & m
        rows_with_failure = int((~combined).sum())
    else:
        rows_with_failure = 0

    overall_pass_rate = (
        (total_rows - rows_with_failure) / total_rows
        if total_rows > 0 else 1.0
    )

    is_promotion_safe = len(blocker_failures) == 0

    # Build blockers/warnings lists
    blockers = [
        f"{r['rule_id']}: {r['failed_count']:,} rows "
        f"failed ({r['failed_rate']:.1%})"
        for r in blocker_failures
    ]
    warnings = [
        f"{r['rule_id']}: {r['failed_count']:,} rows "
        f"failed ({r['failed_rate']:.1%})"
        for r in warning_failures
    ]

    # Recommendation
    recommendation = _build_recommendation(
        is_promotion_safe, blocker_failures, warning_failures
    )

    # Summary
    summary = _build_summary(
        total_rows, rules_passed, rules_failed,
        is_promotion_safe, rows_with_failure,
        rule_results=rule_results,
    )

    # Findings and risks
    findings = _build_findings(rule_results, total_rows)
    if schema_findings:
        findings.extend(schema_findings)
    risks = _build_risks(rule_results, total_rows)
    actions = _build_suggested_next_actions(
        rule_results, is_promotion_safe
    )

    return {
        "kind": "validation_summary_context",
        "subject": subject,
        "summary": summary,
        "metrics": {
            "engine": engine_name,
            "total_rows": total_rows,
            "rules_evaluated": len(rule_results),
            "rules_passed": rules_passed,
            "rules_failed": rules_failed,
            "blocker_count": len(blocker_failures),
            "warning_count": len(warning_failures),
            "rows_with_any_failure": rows_with_failure,
            "overall_pass_rate": round(
                overall_pass_rate, 6
            ),
            "is_promotion_safe": is_promotion_safe,
        },
        "rules": rule_results,
        "blockers": blockers,
        "warnings": warnings,
        "recommendation": recommendation,
        "quarantine_call": quarantine_call,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": actions,
    }


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _rule_columns(rule: dict[str, Any]) -> list[str]:
    """Extract column names from a rule.

    Args:
        rule: Rule dict.

    Returns:
        List of column names.
    """
    col = rule.get("column", "")
    if col:
        return [col]
    return rule.get("columns", [])


def _df_to_records(
    df: Any,
) -> list[dict[str, Any]]:
    """Convert a pandas DataFrame to JSON-safe list of dicts.

    Args:
        df: Pandas DataFrame.

    Returns:
        List of row dicts with JSON-safe values.
    """
    import numpy as np

    records = []
    for _, row in df.iterrows():
        record: dict[str, Any] = {}
        for col in df.columns:
            val = row[col]
            if val is None or (
                isinstance(val, float) and np.isnan(val)
            ):
                record[col] = None
            elif hasattr(val, "item"):
                record[col] = val.item()
            elif hasattr(val, "isoformat"):
                record[col] = val.isoformat()
            else:
                record[col] = val
            if isinstance(record[col], (bytes,)):
                record[col] = record[col].decode(
                    "utf-8", errors="replace"
                )
        records.append(record)
    return records


def _build_summary(
    total_rows: int,
    rules_passed: int,
    rules_failed: int,
    is_safe: bool,
    rows_with_failure: int,
    rule_results: list[dict[str, Any]] | None = None,
) -> str:
    """Build a one-line natural language summary.

    Args:
        total_rows: Total rows.
        rules_passed: Count of passing rules.
        rules_failed: Count of failing rules.
        is_safe: Promotion safety.
        rows_with_failure: Rows with any failure.
        rule_results: Per-rule results for structural/domain classification.

    Returns:
        Summary string.
    """
    total_rules = rules_passed + rules_failed
    if rules_failed == 0:
        return (
            f"All {total_rules} rules pass — data is write-ready. "
            f"({total_rows:,} rows evaluated.)"
        )

    # Classify failures
    failed = [r for r in (rule_results or []) if not r["passed"]]
    structural = [r for r in failed if r.get("rule_type") in _STRUCTURAL_RULE_TYPES]
    domain = [r for r in failed if r not in structural]

    if structural:
        struct_names = ", ".join(r["rule_id"] for r in structural[:5])
        return (
            f"Write blocked: {len(structural)} structural rule(s) failed "
            f"({struct_names}). "
            f"{rows_with_failure:,}/{total_rows:,} rows affected."
        )

    # Only domain failures
    return (
        f"Structural checks pass but {len(domain)} domain rule(s) need review "
        f"— may be acceptable for promotion with documented exceptions. "
        f"({rows_with_failure:,}/{total_rows:,} rows affected.)"
    )


def _build_recommendation(
    is_safe: bool,
    blocker_failures: list[dict[str, Any]],
    warning_failures: list[dict[str, Any]],
) -> str:
    """Build a recommendation string.

    Args:
        is_safe: Whether promotion is safe.
        blocker_failures: Failed blocker rules.
        warning_failures: Failed warning rules.

    Returns:
        Recommendation string.
    """
    if is_safe and not warning_failures:
        return "OK to promote. All rules pass."
    if is_safe and warning_failures:
        return (
            f"OK to promote with {len(warning_failures)} "
            f"warning(s). Review before production."
        )
    return (
        f"BLOCKED. {len(blocker_failures)} blocker(s) must "
        f"be resolved before promotion."
    )


def _build_findings(
    rule_results: list[dict[str, Any]],
    total_rows: int,
) -> list[dict[str, Any]]:
    """Build diagnostic findings from rule results.

    Args:
        rule_results: Per-rule results.
        total_rows: Total row count.

    Returns:
        List of finding dicts.
    """
    findings: list[dict[str, Any]] = []

    if total_rows == 0:
        findings.append({
            "check_type": "empty_dataframe",
            "detail": "DataFrame has 0 rows. Rules pass "
                      "vacuously.",
            "severity": "warning",
        })

    # Classify failed rules into structural vs domain
    failed_rules = [r for r in rule_results if not r["passed"]]
    structural_failures = [
        r for r in failed_rules
        if r.get("rule_type") in _STRUCTURAL_RULE_TYPES
    ]
    domain_failures = [
        r for r in failed_rules if r not in structural_failures
    ]

    # Structural findings — these block writes/merges
    for r in structural_failures:
        cols = ", ".join(r.get("columns", []))
        findings.append({
            "check_type": "structural_constraint_violated",
            "detail": (
                f"Structural constraint violated — {r['rule_type']} on "
                f"{cols or r['rule_id']}. This blocks safe writes/merges."
            ),
            "severity": "blocker",
            "rule_id": r["rule_id"],
            "category": "structural",
        })

    # Domain findings — warn, suggest review
    for r in domain_failures:
        findings.append({
            "check_type": "domain_rule_failure",
            "detail": (
                f"Domain rule {r['rule_id']} fails on "
                f"{r['failed_rate']:.1%} of rows — review whether "
                f"the rule or the data needs updating."
            ),
            "severity": "warning",
            "rule_id": r["rule_id"],
            "category": "domain",
        })

    # High-failure-rate findings (keep existing behaviour for extreme cases)
    for r in failed_rules:
        if r["failed_rate"] > _CRITICAL_FAILURE_RATE:
            findings.append({
                "check_type": "high_failure_rate",
                "detail": (
                    f"Rule {r['rule_id']} fails on "
                    f"{r['failed_rate']:.0%} of rows. "
                    f"Check if rule is too strict or data "
                    f"has systemic issues."
                ),
                "severity": "warning",
            })

    return findings


def _build_risks(
    rule_results: list[dict[str, Any]],
    total_rows: int,
) -> list[dict[str, Any]]:
    """Build risk assessments from rule results.

    Args:
        rule_results: Per-rule results.
        total_rows: Total row count.

    Returns:
        List of risk dicts.
    """
    risks: list[dict[str, Any]] = []

    blockers = [
        r for r in rule_results
        if not r["passed"] and r["severity"] == "blocker"
    ]
    if blockers:
        risks.append({
            "risk": "data_loss_on_write",
            "severity": "high",
            "message": (
                f"{len(blockers)} blocker rule(s) failed. "
                f"Writing to target may lose or corrupt "
                f"data."
            ),
        })

    null_rules = [
        r for r in rule_results
        if r["rule_type"] == "not_null" and not r["passed"]
    ]
    if null_rules:
        affected_cols = []
        for r in null_rules:
            affected_cols.extend(r["columns"])
        risks.append({
            "risk": "null_key_columns",
            "severity": "high",
            "message": (
                f"Null values in columns that should be "
                f"NOT NULL: {affected_cols}. May cause "
                f"join failures or merge conflicts."
            ),
        })

    unique_rules = [
        r for r in rule_results
        if r["rule_type"] == "unique" and not r["passed"]
    ]
    if unique_rules:
        risks.append({
            "risk": "duplicate_keys",
            "severity": "high",
            "message": (
                "Uniqueness violations detected. May cause"
                " row explosion on joins or incorrect "
                "aggregations."
            ),
        })

    # Rate-aware interpretation for each failed rule
    for r in rule_results:
        if r["passed"]:
            continue
        rate = r.get("failed_rate", 0.0)
        rate_pct = rate * 100
        rule_id = r["rule_id"]

        if rate < 0.01:
            interpretation = (
                f"Affects <1% of rows — likely edge cases or data entry errors."
            )
        elif rate <= 0.10:
            interpretation = (
                f"Affects {rate_pct:.1f}% of rows — could indicate a legitimate "
                f"data pattern or a rule that needs updating."
            )
        else:
            interpretation = (
                f"Affects {rate_pct:.1f}% of rows — this suggests either a "
                f"systemic data issue or a rule/contract mismatch. "
                f"Investigate before auto-fixing."
            )

        risks.append({
            "risk": f"failure_rate_{rule_id}",
            "severity": "high" if rate > 0.10 else "medium" if rate > 0.01 else "low",
            "message": f"{rule_id}: {interpretation}",
        })

    return risks


def _build_suggested_next_actions(
    rule_results: list[dict[str, Any]],
    is_safe: bool,
) -> list[str]:
    """Build suggested next actions based on results.

    Args:
        rule_results: Per-rule results.
        is_safe: Whether promotion is safe.

    Returns:
        List of action strings.
    """
    actions: list[str] = []

    # Classify failures
    failed = [r for r in rule_results if not r["passed"]]
    structural_failures = [
        r for r in failed if r.get("rule_type") in _STRUCTURAL_RULE_TYPES
    ]
    domain_failures = [r for r in failed if r not in structural_failures]

    if is_safe:
        actions.append("MUST: Proceed with write/promotion.")
        for r in domain_failures:
            actions.append(
                f"Review rule '{r['rule_id']}' — it may need updating "
                f"if the data pattern is intentional."
            )
        return actions

    # Structural actions — specific per rule type
    for r in structural_failures:
        cols = ", ".join(r.get("columns", []))
        if r["rule_type"] == "not_null":
            actions.append(
                f"MUST: Fix {cols} nulls before write — filter, fill, or "
                f"quarantine rows with split_valid_invalid()."
            )
        elif r["rule_type"] == "unique":
            actions.append(
                f"MUST: Fix {cols} duplicates before write — use "
                f"deduplicate() or investigate upstream duplication."
            )
        elif r["rule_type"] == "primary_key":
            actions.append(
                f"MUST: Fix {cols} primary key violations before write — "
                f"resolve nulls and duplicates."
            )
        elif r["rule_type"] == "foreign_key":
            actions.append(
                f"MUST: Fix {cols} foreign key violations before write — "
                f"ensure all values exist in the reference table."
            )
        else:
            actions.append(
                f"MUST: Resolve structural blocker: {r['rule_id']}."
            )

    # Domain actions — suggest review, not hard fix
    for r in domain_failures:
        actions.append(
            f"Review rule '{r['rule_id']}' — it may need updating "
            f"if the data pattern is intentional."
        )

    if not actions:
        actions.append(
            "MUST: Investigate and resolve blocker rules "
            "before promoting."
        )

    # anchor() workflow hints
    actions.append(
        "MUST: After fixing blockers, re-run anchor('validate', df, rules=[...]) "
        "to confirm all rules pass."
    )
    actions.append(
        "MUST: Run anchor('quality', df, subject='...', keys=[...]) as final "
        "write gate before persisting."
    )
    # ── Graph wiring (audit fix) ──

    actions.append("MUST: Run anchor(\"quality\", df, keys=[...]) for write-safety checks.")

    actions.append("If failures: Run anchor(\"explore\", df) to diagnose data issues.")

    actions.append("MUST: Run anchor(\"touched\") → anchor(\"preflight\") → anchor(\"gate\") after fixes.")

    return actions