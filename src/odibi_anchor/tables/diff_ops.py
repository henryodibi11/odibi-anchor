"""Keyed table diff operations for DataFrames.

Compare two DataFrames for row-level changes at a declared
business-key grain.

All public functions are dual-engine (Spark + Pandas) with
auto-detection.

Usage:
    from odibi_anchor.tables.diff_ops import diff_tables_by_key
    from odibi_anchor.tables.diff_ops import render_diff_report

    ctx = diff_tables_by_key(old_df, new_df, keys=["id"])
    report = render_diff_report(ctx)  # markdown

    # Or inline:
    report = diff_tables_by_key(
        old_df, new_df, keys=["id"], output_format="markdown"
    )
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

logger = logging.getLogger(__name__)

_SUPPORTED_DIFF_ENGINES = {"auto", "pandas", "spark"}

def diff_tables_by_key(
    old_df: Any,
    new_df: Any,
    keys: list[str],
    *,
    compare_columns: list[str] | None = None,
    subject: str | None = None,
    engine: str = "auto",
    sample_limit: int = 20,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Compare two DataFrames by business key and return change context.

    ``diff_tables_by_key`` compares two table snapshots at a declared business
    grain. It reports added keys, removed keys, changed rows, unchanged rows,
    changed-column counts, schema drift, dtype drift, capped evidence samples,
    and suggested next actions.

    The pandas backend is complete. The public API intentionally includes an
    engine argument so the Spark backend can be implemented in Databricks behind
    the same contract.

    Args:
        old_df: Previous/source DataFrame.
        new_df: Current/target DataFrame.
        keys: Columns that define the expected business grain.
        compare_columns: Optional non-key columns to compare. When omitted,
            common non-key columns are compared.
        subject: Optional label such as a table name, version pair, or pipeline
            step. Defaults to ``"old_df vs new_df"``.
        engine: Execution engine. Use ``"auto"``, ``"pandas"``, or ``"spark"``.
        sample_limit: Maximum examples to include per sample section. Also caps
            per-row changed-column details in ``changed_rows`` samples.

    Returns:
        Structured context dictionary with ``kind``, ``subject``, ``engine``,
        ``status``, ``summary``, ``metrics``, ``columns``, ``findings``,
        ``risks``, ``samples``, and ``suggested_next_actions``.

    Raises:
        ValueError: If arguments or required columns are invalid.
        TypeError: If the selected engine does not match the input objects.

    New in v2:
        - ``output_format="markdown"`` returns rendered report.
        - ``metrics`` includes ``added_key_pct``, ``removed_key_pct``,
          ``changed_key_pct`` for instant threshold decisions.
        - ``merge_expr`` (when status is "ok") contains a ready-to-paste
          MERGE INTO SQL statement.

    Example:
        >>> import pandas as pd
        >>> old = pd.DataFrame({"id": [1, 2], "status": ["new", "active"]})
        >>> new = pd.DataFrame({"id": [1, 2, 3], "status": ["new", "retired", "new"]})
        >>> diff = diff_tables_by_key(old, new, keys=["id"], subject="customer_snapshot")
        >>> diff["metrics"]["added_key_count"]
        1
        >>> diff["metrics"]["changed_key_count"]
        1
    """
    _validate_engine_arg(engine)
    _validate_sample_limit(sample_limit)
    keys = _validate_column_name_list("keys", keys, allow_empty=False)
    if compare_columns is not None:
        compare_columns = _validate_column_name_list("compare_columns", compare_columns, allow_empty=True)

    if engine == "auto":
        old_engine = detect_engine(old_df)
        new_engine = detect_engine(new_df)
        if old_engine != new_engine:
            raise ValueError(f"old_df and new_df must use the same engine. Got old={old_engine!r}, new={new_engine!r}.")
        selected_engine = old_engine
    else:
        selected_engine = engine

    validate_output_format(output_format)

    if selected_engine == "pandas":
        ctx = _diff_tables_by_key_pandas(
            old_df=old_df,
            new_df=new_df,
            keys=keys,
            compare_columns=compare_columns,
            subject=subject,
            sample_limit=sample_limit,
        )
    elif selected_engine == "spark":
        ctx = _diff_tables_by_key_spark(
            old_df=old_df,
            new_df=new_df,
            keys=keys,
            compare_columns=compare_columns,
            subject=subject,
            sample_limit=sample_limit,
        )
    else:
        raise TypeError(
            "Unsupported DataFrame engine for "
            "diff_tables_by_key."
        )

    if output_format == "markdown":
        return render_diff_report(ctx)
    return ctx



def render_diff_report(
    ctx: dict[str, Any],
    *,
    show_samples: bool = True,
) -> str:
    """Render a diff context dict as structured markdown.

    Designed for LLM prompts and human consumption. Produces a
    compact, token-efficient report with clear sections.

    Args:
        ctx: The dictionary returned by ``diff_tables_by_key()``.
        show_samples: If True, include changed-row evidence.

    Returns:
        A markdown-formatted string.

    Raises:
        ValueError: If ``ctx`` is missing required keys.
    """
    required = {"kind", "subject", "status", "summary", "metrics"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(
            f"Context dict missing keys: {sorted(missing)}"
        )

    lines: list[str] = []
    m = ctx["metrics"]
    status = ctx["status"]

    icon = "\u2705" if status == "ok" else "\U0001f6d1"
    lines.append(f"# Diff: {ctx['subject']}")
    lines.append("")
    lines.append(f"{icon} **Status:** {status.upper()}")
    lines.append(f"**Engine:** {ctx['engine']}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    change_pattern = ctx.get("change_pattern")
    if change_pattern:
        lines.append(f"**Pattern:** {change_pattern['pattern']}")
        lines.append(f"> {change_pattern['interpretation']}")
        lines.append("")

    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Old rows | {m['old_row_count']:,} |")
    lines.append(f"| New rows | {m['new_row_count']:,} |")
    lines.append(
        f"| Row delta | {m['row_count_delta']:+,} |"
    )
    if m.get("added_key_count") is not None:
        lines.append(
            f"| Added keys | "
            f"{m['added_key_count']:,} |"
        )
        lines.append(
            f"| Removed keys | "
            f"{m['removed_key_count']:,} |"
        )
        lines.append(
            f"| Changed keys | "
            f"{m['changed_key_count']:,} |"
        )
        lines.append(
            f"| Unchanged keys | "
            f"{m['unchanged_key_count']:,} |"
        )
    if m.get("changed_cell_count") is not None:
        lines.append(
            f"| Changed cells | "
            f"{m['changed_cell_count']:,} |"
        )
    lines.append("")

    cc_list = m.get("changed_column_counts", [])
    if cc_list:
        lines.append("## Changed Columns")
        lines.append("")
        lines.append("| Column | Keys Changed | null→value | value→null | value≠value |")
        lines.append("| --- | --- | --- | --- | --- |")
        for cc in cc_list:
            lines.append(
                f"| {cc['column']} | "
                f"{cc['changed_key_count']:,} | "
                f"{cc.get('null_to_value', 0):,} | "
                f"{cc.get('value_to_null', 0):,} | "
                f"{cc.get('value_changed', 0):,} |"
            )
        lines.append("")

    findings = ctx.get("findings", [])
    if findings:
        lines.append("## Findings")
        lines.append("")
        for f in findings:
            sev = f.get("severity", "info")
            sev_icon = (
                "\U0001f6d1" if sev == "high"
                else "\u26a0\ufe0f" if sev == "medium"
                else "\u2139\ufe0f"
            )
            msg = f.get("message", f.get("detail", ""))
            lines.append(f"* {sev_icon} {msg}")
        lines.append("")

    risks = ctx.get("risks", [])
    if risks:
        lines.append("## Risks")
        lines.append("")
        for r in risks:
            sev = r.get("severity", "medium")
            sev_icon = (
                "\U0001f6d1" if sev == "high"
                else "\u26a0\ufe0f" if sev == "medium"
                else "\u2139\ufe0f"
            )
            lines.append(
                f"* {sev_icon} **{r.get('type', '')}**: "
                f"{r.get('message', '')}"
            )
        lines.append("")

    if show_samples:
        samples = ctx.get("samples", {})
        changed = samples.get("changed_rows", [])
        if changed:
            lines.append("## Changed Row Evidence")
            lines.append("")
            lines.append("```")
            for row in changed[:10]:
                key_str = ", ".join(
                    f"{k}={v}"
                    for k, v in row["key"].items()
                )
                changes_str = ", ".join(
                    f"{c}: {v['old']}\u2192{v['new']}"
                    for c, v in row["changes"].items()
                )
                lines.append(
                    f"  [{key_str}] {changes_str}"
                )
            lines.append("```")
            lines.append("")

    actions = ctx.get("suggested_next_actions", [])
    if actions:
        lines.append("## Next Actions")
        lines.append("")
        for a in actions:
            lines.append(f"* {a}")
        lines.append("")

    return "\n".join(lines)


def _validate_engine_arg(engine: str) -> None:
    """Raise ValueError if engine is not a supported value."""
    if engine not in _SUPPORTED_DIFF_ENGINES:
        allowed = ", ".join(sorted(_SUPPORTED_DIFF_ENGINES))
        raise ValueError(f"Unsupported engine {engine!r}. Expected one of: {allowed}.")


def _validate_sample_limit(sample_limit: int) -> None:
    """Raise ValueError if sample_limit is negative."""
    if not isinstance(sample_limit, int) or isinstance(sample_limit, bool):
        raise ValueError("sample_limit must be a non-negative integer.")
    if sample_limit < 0:
        raise ValueError("sample_limit must be a non-negative integer.")


def _validate_column_name_list(name: str, columns: Any, *, allow_empty: bool) -> list[str]:
    """Validate and normalize a column name list."""
    if isinstance(columns, str) or not isinstance(columns, list):
        raise ValueError(f"{name} must be a list of column names.")
    if not allow_empty and not columns:
        raise ValueError(f"{name} must contain at least one column.")

    non_strings = [column for column in columns if not isinstance(column, str)]
    if non_strings:
        raise ValueError(f"{name} must contain only string column names: {non_strings!r}")

    duplicates = _duplicate_values(columns)
    if duplicates:
        raise ValueError(f"{name} must not contain duplicate column names: {duplicates!r}")
    return list(columns)


def _diff_tables_by_key_pandas(
    old_df: Any,
    new_df: Any,
    keys: list[str],
    compare_columns: list[str] | None,
    subject: str | None,
    sample_limit: int,
) -> dict[str, Any]:
    """Pandas implementation for diff_tables_by_key."""
    import pandas as pd

    if not isinstance(old_df, pd.DataFrame) or not isinstance(new_df, pd.DataFrame):
        raise TypeError("engine='pandas' requires both old_df and new_df to be pandas DataFrames.")

    subject_label = subject or "old_df vs new_df"
    old_columns = list(old_df.columns)
    new_columns = list(new_df.columns)
    _validate_no_duplicate_dataframe_columns("old_df", old_columns)
    _validate_no_duplicate_dataframe_columns("new_df", new_columns)

    compare_cols = _resolve_compare_columns(
        old_columns=old_columns,
        new_columns=new_columns,
        keys=keys,
        compare_columns=compare_columns,
    )

    old_only_columns = [col for col in old_columns if col not in new_columns]
    new_only_columns = [col for col in new_columns if col not in old_columns]
    common_columns = [col for col in old_columns if col in new_columns]
    key_dtype_changes = _dtype_changes_pandas(old_df, new_df, keys, require_observed_values=True)
    compare_dtype_changes = _dtype_changes_pandas(old_df, new_df, compare_cols, require_observed_values=False)
    normalized_key_dtype_changes = _normalize_dtype_changes(
        key_dtype_changes,
        is_key=True,
        is_compared=False,
    )
    normalized_compare_dtype_changes = _normalize_dtype_changes(
        compare_dtype_changes,
        is_key=False,
        is_compared=True,
    )
    type_changed_columns = normalized_key_dtype_changes + normalized_compare_dtype_changes
    columns_context = _columns_context(
        keys=keys,
        compare_cols=compare_cols,
        old_only_columns=old_only_columns,
        new_only_columns=new_only_columns,
        common_columns=common_columns,
        type_changed_columns=type_changed_columns,
    )
    old_duplicates = _duplicate_key_summary_pandas(old_df, keys, sample_limit)
    new_duplicates = _duplicate_key_summary_pandas(new_df, keys, sample_limit)
    old_null_keys = _null_key_summary_pandas(old_df, keys, sample_limit)
    new_null_keys = _null_key_summary_pandas(new_df, keys, sample_limit)

    base_metrics = {
        "old_row_count": int(len(old_df)),
        "new_row_count": int(len(new_df)),
        "row_count_delta": int(len(new_df) - len(old_df)),
        "old_key_count": int(old_df[keys].drop_duplicates().shape[0]),
        "new_key_count": int(new_df[keys].drop_duplicates().shape[0]),
        "common_key_count": None,
        "added_key_count": None,
        "removed_key_count": None,
        "changed_key_count": None,
        "unchanged_key_count": None,
        "has_changes": None,
        "compared_column_count": int(len(compare_cols)),
        "common_column_count": int(len(common_columns)),
        "old_only_column_count": int(len(old_only_columns)),
        "new_only_column_count": int(len(new_only_columns)),
        "key_dtype_change_count": int(len(key_dtype_changes)),
        "compare_dtype_change_count": int(len(compare_dtype_changes)),
        "type_changed_column_count": int(len(type_changed_columns)),
        "old_duplicate_key_count": int(old_duplicates["duplicate_key_count"]),
        "old_duplicate_row_count": int(old_duplicates["duplicate_row_count"]),
        "new_duplicate_key_count": int(new_duplicates["duplicate_key_count"]),
        "new_duplicate_row_count": int(new_duplicates["duplicate_row_count"]),
        "old_null_key_row_count": int(old_null_keys["null_key_row_count"]),
        "new_null_key_row_count": int(new_null_keys["null_key_row_count"]),
        "changed_column_counts": [],
        "changed_cell_count": None,
    }

    schema_findings = _schema_findings(
        old_only_columns=old_only_columns,
        new_only_columns=new_only_columns,
        compare_dtype_changes=compare_dtype_changes,
        normalized_compare_dtype_changes=normalized_compare_dtype_changes,
        type_changed_columns=type_changed_columns,
    )
    blocking_risks = _blocking_diff_risks(
        old_duplicates=old_duplicates,
        new_duplicates=new_duplicates,
        old_null_keys=old_null_keys,
        new_null_keys=new_null_keys,
        normalized_key_dtype_changes=normalized_key_dtype_changes,
    )

    if blocking_risks:
        samples = _empty_diff_samples()
        samples.update(
            {
                "old_duplicate_keys": old_duplicates["samples"],
                "new_duplicate_keys": new_duplicates["samples"],
                "old_null_key_rows": old_null_keys["samples"],
                "new_null_key_rows": new_null_keys["samples"],
            }
        )
        return _build_context(
            subject=subject_label,
            engine="pandas",
            status="blocked",
            summary=_blocked_summary(subject_label, blocking_risks),
            metrics=base_metrics,
            columns=columns_context,
            findings=schema_findings,
            risks=blocking_risks,
            samples=samples,
            suggested_next_actions=_blocked_next_actions(blocking_risks),
        )

    joined = old_df[keys + compare_cols].merge(
        new_df[keys + compare_cols],
        on=keys,
        how="outer",
        suffixes=("__old", "__new"),
        indicator=True,
        sort=False,
    )

    added_mask = joined["_merge"].eq("right_only")
    removed_mask = joined["_merge"].eq("left_only")
    common_mask = joined["_merge"].eq("both")
    column_change_masks = _column_change_masks_pandas(joined, common_mask, compare_cols)

    if column_change_masks:
        changed_mask = _combine_boolean_masks(column_change_masks, index=joined.index, default=False) & common_mask
    else:
        changed_mask = _false_mask_like(joined)
    unchanged_mask = common_mask & ~changed_mask

    added_key_count = int(added_mask.sum())
    removed_key_count = int(removed_mask.sum())
    common_key_count = int(common_mask.sum())
    changed_key_count = int(changed_mask.sum())
    unchanged_key_count = int(unchanged_mask.sum())
    changed_column_counts = _changed_column_counts_categorized(column_change_masks, joined)
    changed_cell_count = int(sum(item["changed_key_count"] for item in changed_column_counts))
    has_changes = bool(
        added_key_count
        or removed_key_count
        or changed_key_count
        or old_only_columns
        or new_only_columns
        or key_dtype_changes
        or compare_dtype_changes
    )

    # Percentage metrics (v2)
    _old_kc = int(base_metrics["old_key_count"]) or 1
    added_key_pct = round(added_key_count / _old_kc, 4)
    removed_key_pct = round(removed_key_count / _old_kc, 4)
    changed_key_pct = round(
        changed_key_count / _old_kc, 4
    )

    metrics = dict(base_metrics)
    metrics.update(
        {
            "common_key_count": common_key_count,
            "added_key_count": added_key_count,
            "removed_key_count": removed_key_count,
            "changed_key_count": changed_key_count,
            "unchanged_key_count": unchanged_key_count,
            "has_changes": has_changes,
            "changed_column_counts": changed_column_counts,
            "changed_cell_count": changed_cell_count,
            "added_key_pct": added_key_pct,
            "removed_key_pct": removed_key_pct,
            "changed_key_pct": changed_key_pct,
        }
    )

    added_rows = joined.loc[added_mask]
    removed_rows = joined.loc[removed_mask]
    samples = _empty_diff_samples()
    samples.update(
        {
            "added_keys": _sample_key_records_pandas(added_rows, keys, sample_limit),
            "removed_keys": _sample_key_records_pandas(removed_rows, keys, sample_limit),
            "added_rows": _sample_side_rows_from_joined_pandas(added_rows, keys, compare_cols, "new", sample_limit),
            "removed_rows": _sample_side_rows_from_joined_pandas(removed_rows, keys, compare_cols, "old", sample_limit),
            "changed_rows": _sample_changed_rows_pandas(joined, changed_mask, column_change_masks, keys, sample_limit),
        }
    )

    findings = (
        _change_findings(
            added_key_count=added_key_count,
            removed_key_count=removed_key_count,
            changed_key_count=changed_key_count,
            unchanged_key_count=unchanged_key_count,
            changed_column_counts=changed_column_counts,
            compare_column_count=len(compare_cols),
        )
        + schema_findings
    )
    risks = _non_blocking_diff_risks(
        removed_key_count=removed_key_count,
        old_only_columns=old_only_columns,
        new_only_columns=new_only_columns,
        compare_dtype_changes=compare_dtype_changes,
        normalized_compare_dtype_changes=normalized_compare_dtype_changes,
        compared_column_count=len(compare_cols),
        common_key_count=common_key_count,
        old_key_count=int(base_metrics["old_key_count"]),
        new_key_count=int(base_metrics["new_key_count"]),
    )

    merge_expr = _build_merge_expr(
        keys=keys,
        compare_cols=compare_cols,
        subject=subject_label,
        engine="pandas",
    )

    cc_dict = {item["column"]: item for item in changed_column_counts}
    change_pattern = _classify_change_pattern(
        added_count=added_key_count,
        removed_count=removed_key_count,
        changed_count=changed_key_count,
        unchanged_count=unchanged_key_count,
        total_old=int(base_metrics["old_key_count"]),
        total_new=int(base_metrics["new_key_count"]),
        changed_column_counts=cc_dict if cc_dict else None,
    )

    ctx = _build_context(
        subject=subject_label,
        engine="pandas",
        status="ok",
        merge_expr=merge_expr,
        summary=_ok_summary(
            subject_label=subject_label,
            keys=keys,
            old_key_count=int(metrics["old_key_count"]),
            new_key_count=int(metrics["new_key_count"]),
            added_key_count=added_key_count,
            removed_key_count=removed_key_count,
            changed_key_count=changed_key_count,
        ),
        metrics=metrics,
        columns=columns_context,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=_next_actions(
            added_key_count=added_key_count,
            removed_key_count=removed_key_count,
            changed_key_count=changed_key_count,
            changed_column_counts=changed_column_counts,
            old_only_columns=old_only_columns,
            new_only_columns=new_only_columns,
            compare_dtype_changes=compare_dtype_changes,
        ),
    )
    ctx["change_pattern"] = change_pattern
    return ctx


def _resolve_compare_columns(
    old_columns: list[str],
    new_columns: list[str],
    keys: list[str],
    compare_columns: list[str] | None,
) -> list[str]:
    """Resolve which non-key columns to compare."""
    missing_old_keys = [col for col in keys if col not in old_columns]
    missing_new_keys = [col for col in keys if col not in new_columns]
    if missing_old_keys or missing_new_keys:
        messages = []
        if missing_old_keys:
            messages.append(f"old_df is missing required key columns: {missing_old_keys}")
        if missing_new_keys:
            messages.append(f"new_df is missing required key columns: {missing_new_keys}")
        raise ValueError("Key columns are missing: " + "; ".join(messages))

    if compare_columns is None:
        return [col for col in old_columns if col in new_columns and col not in keys]

    key_compare_columns = [col for col in compare_columns if col in keys]
    if key_compare_columns:
        raise ValueError(f"compare_columns cannot include key columns: {key_compare_columns}")

    missing_old_compare = [col for col in compare_columns if col not in old_columns]
    missing_new_compare = [col for col in compare_columns if col not in new_columns]
    if missing_old_compare or missing_new_compare:
        messages = []
        if missing_old_compare:
            messages.append(f"old_df is missing required compare columns: {missing_old_compare}")
        if missing_new_compare:
            messages.append(f"new_df is missing required compare columns: {missing_new_compare}")
        raise ValueError("Compare columns are missing: " + "; ".join(messages))

    return list(compare_columns)


def _validate_no_duplicate_dataframe_columns(df_name: str, columns: list[Any]) -> None:
    """Raise ValueError if a DataFrame has duplicate column names."""
    duplicates = _duplicate_values(columns)
    if duplicates:
        raise ValueError(f"{df_name} contains duplicate column names: {duplicates!r}")


def _duplicate_values(values: list[Any]) -> list[Any]:
    """Return values that appear more than once."""
    seen = set()
    duplicates = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _duplicate_key_summary_pandas(df: Any, keys: list[str], sample_limit: int) -> dict[str, Any]:
    """Detect duplicate keys in a pandas DataFrame."""
    if len(df) == 0:
        return {"duplicate_key_count": 0, "duplicate_row_count": 0, "samples": []}

    grouped = df.groupby(keys, dropna=False, sort=False).size().reset_index(name="row_count")
    duplicates = grouped[grouped["row_count"] > 1]
    return {
        "duplicate_key_count": int(len(duplicates)),
        "duplicate_row_count": int(duplicates["row_count"].sum()) if len(duplicates) else 0,
        "samples": _records_to_json_safe(duplicates.head(sample_limit).to_dict("records")),
    }


def _null_key_summary_pandas(df: Any, keys: list[str], sample_limit: int) -> dict[str, Any]:
    """Detect null key values in a pandas DataFrame."""
    if len(df) == 0:
        return {"null_key_row_count": 0, "samples": []}

    null_mask = df[keys].isna().any(axis=1)
    null_rows = df.loc[null_mask, keys].head(sample_limit).copy()
    samples = []
    for row_index, row in null_rows.iterrows():
        sample = {"row_index": _json_safe_value(row_index)}
        sample.update({key: _json_safe_value(row[key]) for key in keys})
        samples.append(sample)
    return {
        "null_key_row_count": int(null_mask.sum()),
        "samples": samples,
    }


def _dtype_changes_pandas(
    old_df: Any,
    new_df: Any,
    columns: list[str],
    *,
    require_observed_values: bool,
) -> list[dict[str, str]]:
    """Detect dtype differences between pandas DataFrames."""
    changes = []
    for col in columns:
        if col not in old_df.columns or col not in new_df.columns:
            continue
        if require_observed_values:
            old_has_values = bool(old_df[col].notna().any()) if len(old_df) else False
            new_has_values = bool(new_df[col].notna().any()) if len(new_df) else False
            if not (old_has_values and new_has_values):
                continue
        old_dtype = str(old_df[col].dtype)
        new_dtype = str(new_df[col].dtype)
        if old_dtype != new_dtype:
            changes.append({"column": col, "old_dtype": old_dtype, "new_dtype": new_dtype})
    return changes


def _normalize_dtype_changes(
    changes: list[dict[str, str]],
    *,
    is_key: bool,
    is_compared: bool,
) -> list[dict[str, Any]]:
    """Normalize raw dtype change dicts into standard shape."""
    return [
        {
            "column": change["column"],
            "old_type": change.get("old_type", change.get("old_dtype")),
            "new_type": change.get("new_type", change.get("new_dtype")),
            "is_key": is_key,
            "is_compared": is_compared,
        }
        for change in changes
    ]


def _columns_context(
    *,
    keys: list[str],
    compare_cols: list[str],
    old_only_columns: list[str],
    new_only_columns: list[str],
    common_columns: list[str],
    type_changed_columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build column-level metadata for the diff context."""
    return {
        "keys": list(keys),
        "compared": list(compare_cols),
        "common_columns": list(common_columns),
        "old_only_columns": list(old_only_columns),
        "new_only_columns": list(new_only_columns),
        "type_changes": list(type_changed_columns),
    }


def _schema_findings(
    old_only_columns: list[str],
    new_only_columns: list[str],
    compare_dtype_changes: list[dict[str, str]],
    normalized_compare_dtype_changes: list[dict[str, Any]],
    type_changed_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Generate findings for schema-level differences."""
    findings: list[dict[str, Any]] = []
    if old_only_columns:
        findings.append(
            {
                "type": "columns_only_in_old",
                "severity": "warning",
                "count": len(old_only_columns),
                "columns": old_only_columns,
                "message": f"{len(old_only_columns)} columns exist only in old_df and were not row-compared.",
            }
        )
    if new_only_columns:
        findings.append(
            {
                "type": "columns_only_in_new",
                "severity": "warning",
                "count": len(new_only_columns),
                "columns": new_only_columns,
                "message": f"{len(new_only_columns)} columns exist only in new_df and were not row-compared.",
            }
        )

    if compare_dtype_changes:
        findings.append(
            {
                "type": "compare_dtype_changes",
                "severity": "warning",
                "count": len(compare_dtype_changes),
                "columns": list(normalized_compare_dtype_changes),
                "message": "Compared column dtype changes were detected.",
            }
        )

    if type_changed_columns:
        findings.append(
            {
                "type": "type_changed_columns",
                "severity": "warning",
                "count": len(type_changed_columns),
                "columns": list(type_changed_columns),
                "message": "Column dtype changes were detected.",
            }
        )
    return findings


def _blocking_diff_risks(
    old_duplicates: dict[str, Any],
    new_duplicates: dict[str, Any],
    old_null_keys: dict[str, Any],
    new_null_keys: dict[str, Any],
    normalized_key_dtype_changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Identify risks that block comparison entirely."""
    risks: list[dict[str, Any]] = []
    if old_duplicates["duplicate_key_count"]:
        risks.append(
            {
                "severity": "high",
                "type": "duplicate_keys_old_df",
                "message": (
                    "old_df contains duplicate business keys; row-level diff would create "
                    "ambiguous comparisons."
                ),
                "count": int(old_duplicates["duplicate_key_count"]),
            }
        )
    if new_duplicates["duplicate_key_count"]:
        risks.append(
            {
                "severity": "high",
                "type": "duplicate_keys_new_df",
                "message": (
                    "new_df contains duplicate business keys; row-level diff would create "
                    "ambiguous comparisons."
                ),
                "count": int(new_duplicates["duplicate_key_count"]),
            }
        )
    if old_null_keys["null_key_row_count"]:
        risks.append(
            {
                "severity": "high",
                "type": "null_keys_old_df",
                "message": "old_df contains null key values; row-level diff requires complete keys.",
                "count": int(old_null_keys["null_key_row_count"]),
            }
        )
    if new_null_keys["null_key_row_count"]:
        risks.append(
            {
                "severity": "high",
                "type": "null_keys_new_df",
                "message": "new_df contains null key values; row-level diff requires complete keys.",
                "count": int(new_null_keys["null_key_row_count"]),
            }
        )
    if normalized_key_dtype_changes:
        risks.append(
            {
                "severity": "high",
                "type": "key_dtype_changes",
                "message": (
                    "Key column dtype changes can make exact key matching unreliable; "
                    "align key types before diffing."
                ),
                "columns": list(normalized_key_dtype_changes),
                "count": len(normalized_key_dtype_changes),
            }
        )
    return risks


def _non_blocking_diff_risks(
    removed_key_count: int,
    old_only_columns: list[str],
    new_only_columns: list[str],
    compare_dtype_changes: list[dict[str, str]],
    normalized_compare_dtype_changes: list[dict[str, Any]],
    compared_column_count: int,
    common_key_count: int,
    old_key_count: int,
    new_key_count: int,
) -> list[dict[str, Any]]:
    """Identify risks that warn but allow comparison."""
    risks: list[dict[str, Any]] = []
    if removed_key_count:
        risks.append(
            {
                "severity": "medium",
                "type": "removed_keys",
                "message": (
                    "Keys disappeared from new_df. Confirm this is expected before using "
                    "the new output downstream."
                ),
                "count": removed_key_count,
            }
        )
    if old_only_columns or new_only_columns:
        risks.append(
            {
                "severity": "medium",
                "type": "schema_drift",
                "message": "One-sided columns were detected and excluded from row-level value comparison.",
                "old_only_column_count": len(old_only_columns),
                "new_only_column_count": len(new_only_columns),
            }
        )
    if compare_dtype_changes:
        risks.append(
            {
                "severity": "medium",
                "type": "compare_dtype_changes",
                "message": (
                    "Compared column dtypes changed. Confirm this is expected before "
                    "interpreting value-level changes."
                ),
                "columns": list(normalized_compare_dtype_changes),
                "count": len(compare_dtype_changes),
            }
        )
    if compared_column_count == 0:
        risks.append(
            {
                "severity": "low",
                "type": "no_compare_columns",
                "message": "No non-key columns were compared; only key presence was evaluated.",
            }
        )
    if compared_column_count > 50:
        risks.append(
            {
                "severity": "low",
                "type": "wide_table_comparison",
                "message": (
                    "More than 50 columns were compared. Review changed-column summaries "
                    "before inspecting row samples."
                ),
                "count": compared_column_count,
            }
        )
    if common_key_count == 0 and old_key_count and new_key_count:
        risks.append(
            {
                "severity": "medium",
                "type": "no_common_keys",
                "message": "No overlapping keys were found between old_df and new_df.",
            }
        )
    return risks


def _change_findings(
    added_key_count: int,
    removed_key_count: int,
    changed_key_count: int,
    unchanged_key_count: int,
    changed_column_counts: list[dict[str, Any]],
    compare_column_count: int,
) -> list[dict[str, Any]]:
    """Generate findings for row-level change statistics."""
    findings: list[dict[str, Any]] = [
        {
            "type": "added_keys",
            "severity": "info",
            "count": added_key_count,
            "message": f"{added_key_count} keys exist only in new_df.",
        },
        {
            "type": "removed_keys",
            "severity": "info",
            "count": removed_key_count,
            "message": f"{removed_key_count} keys exist only in old_df.",
        },
        {
            "type": "changed_rows",
            "severity": "info",
            "count": changed_key_count,
            "message": f"{changed_key_count} common keys have at least one changed compared column.",
        },
        {
            "type": "unchanged_rows",
            "severity": "info",
            "count": unchanged_key_count,
            "message": f"{unchanged_key_count} common keys have no changed compared columns.",
        },
    ]
    if added_key_count == 0 and removed_key_count == 0 and changed_key_count == 0:
        findings.append(
            {
                "type": "no_differences",
                "severity": "info",
                "message": "No key-level or value-level row differences were detected.",
            }
        )
    if changed_column_counts:
        findings.append(
            {
                "type": "top_changed_columns",
                "severity": "info",
                "columns": changed_column_counts,
                "message": "Columns ranked by number of changed keys.",
            }
        )
    if compare_column_count == 0:
        findings.append(
            {
                "type": "no_compare_columns",
                "severity": "warning",
                "message": "No non-key columns were compared; output only reflects key-level added/removed counts.",
            }
        )
    return findings


def _column_change_masks_pandas(joined: Any, common_mask: Any, compare_columns: list[str]) -> dict[str, Any]:
    """Build per-column boolean masks for changed values."""
    masks: dict[str, Any] = {}
    for col in compare_columns:
        old_col = f"{col}__old"
        new_col = f"{col}__new"
        equal_values = joined[old_col].eq(joined[new_col]).fillna(False)
        both_null = joined[old_col].isna() & joined[new_col].isna()
        masks[col] = common_mask & ~(equal_values | both_null)
    return masks


def _combine_boolean_masks(masks: dict[str, Any], *, index: Any, default: bool) -> Any:
    """OR-combine multiple boolean masks into one."""
    import pandas as pd

    combined = pd.Series(default, index=index)
    for mask in masks.values():
        combined = combined | mask
    return combined


def _false_mask_like(df: Any) -> Any:
    """Return an all-False boolean Series matching df index."""
    import pandas as pd

    return pd.Series(False, index=df.index)


def _changed_column_counts_categorized(
    column_change_masks: dict[str, Any],
    joined: Any,
) -> list[dict[str, Any]]:
    """Count changed keys per column with NULL-aware categorization.

    For each column with changes, reports:
      - null_to_value: old was NULL, new has a value
      - value_to_null: old had a value, new is NULL
      - value_changed: both had non-NULL values that differ
      - changed_key_count: total (sum of above three)
    """
    counts = []
    for col, mask in column_change_masks.items():
        total = int(mask.sum())
        if total == 0:
            continue

        old_col = f"{col}__old"
        new_col = f"{col}__new"

        changed_rows = joined.loc[mask]
        old_null = changed_rows[old_col].isna()
        new_null = changed_rows[new_col].isna()

        null_to_value = int((old_null & ~new_null).sum())
        value_to_null = int((~old_null & new_null).sum())
        value_changed = int((~old_null & ~new_null).sum())

        counts.append({
            "column": col,
            "changed_key_count": total,
            "null_to_value": null_to_value,
            "value_to_null": value_to_null,
            "value_changed": value_changed,
        })

    return sorted(counts, key=lambda item: (-item["changed_key_count"], item["column"]))


def _empty_diff_samples() -> dict[str, list[dict[str, Any]]]:
    """Return an empty samples dict with all expected keys."""
    return {
        "old_duplicate_keys": [],
        "new_duplicate_keys": [],
        "old_null_key_rows": [],
        "new_null_key_rows": [],
        "added_keys": [],
        "removed_keys": [],
        "added_rows": [],
        "removed_rows": [],
        "changed_rows": [],
    }


def _sample_key_records_pandas(df: Any, keys: list[str], sample_limit: int) -> list[dict[str, Any]]:
    """Collect capped key-only records from pandas."""
    if sample_limit == 0 or len(df) == 0:
        return []
    return _records_to_json_safe(df.loc[:, keys].head(sample_limit).to_dict("records"))


def _sample_side_rows_from_joined_pandas(
    df: Any,
    keys: list[str],
    compare_columns: list[str],
    side: str,
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Collect capped full-row samples from one side."""
    if sample_limit == 0 or len(df) == 0:
        return []

    rows = []
    suffix = f"__{side}"
    for _, row in df.head(sample_limit).iterrows():
        rows.append(
            {
                "key": {key: _json_safe_value(row[key]) for key in keys},
                "values": {col: _json_safe_value(row[f"{col}{suffix}"]) for col in compare_columns},
            }
        )
    return rows


def _sample_changed_rows_pandas(
    joined: Any,
    changed_mask: Any,
    column_change_masks: dict[str, Any],
    keys: list[str],
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Collect capped changed-row evidence with diffs."""
    if sample_limit == 0 or not bool(changed_mask.any()):
        return []

    rows = []
    changed_rows = joined.loc[changed_mask].head(sample_limit)
    for index, row in changed_rows.iterrows():
        changed_columns = [col for col, mask in column_change_masks.items() if bool(mask.loc[index])]
        shown_columns = changed_columns[:sample_limit]
        changes = {
            col: {
                "old": _json_safe_value(row[f"{col}__old"]),
                "new": _json_safe_value(row[f"{col}__new"]),
            }
            for col in shown_columns
        }
        rows.append(
            {
                "key": {key: _json_safe_value(row[key]) for key in keys},
                "changed_column_count": len(changed_columns),
                "changed_columns": shown_columns,
                "changes": changes,
                "truncated_changed_columns": len(changed_columns) > len(shown_columns),
            }
        )
    return rows


def _records_to_json_safe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a list of dicts to JSON-safe values."""
    return [{str(key): _json_safe_value(value) for key, value in record.items()} for record in records]


def _json_safe_value(value: Any) -> Any:
    """Convert a single value to a JSON-serializable type."""
    if value is None:
        return None

    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    try:
        import pandas as pd

        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "item"):
        try:
            return _json_safe_value(value.item())
        except (TypeError, ValueError, OverflowError):
            pass

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def _blocked_summary(subject_label: str, risks: list[dict[str, Any]]) -> str:
    """Build a one-line summary for blocked comparisons."""
    risk_types = ", ".join(risk["type"] for risk in risks)
    return f"Comparison blocked for {subject_label}: {risk_types}."


def _ok_summary(
    subject_label: str,
    keys: list[str],
    old_key_count: int,
    new_key_count: int,
    added_key_count: int,
    removed_key_count: int,
    changed_key_count: int,
) -> str:
    """Build a one-line summary for successful comparisons."""
    return (
        f"Compared {old_key_count} old keys to {new_key_count} new keys for {subject_label} "
        f"using keys {keys}; {added_key_count} added, {removed_key_count} removed, "
        f"{changed_key_count} changed."
    )


def _blocked_next_actions(risks: list[dict[str, Any]]) -> list[str]:
    """Generate next-action suggestions for blocked state."""
    actions: list[str] = []
    risk_types = {risk["type"] for risk in risks}
    if "duplicate_keys_old_df" in risk_types or "duplicate_keys_new_df" in risk_types:
        actions.append("Deduplicate each DataFrame at the declared key grain before running diff_tables_by_key.")
        actions.append("Confirm that keys represent the intended business grain.")
    if "null_keys_old_df" in risk_types or "null_keys_new_df" in risk_types:
        actions.append("Filter, impute, or quarantine rows with null key values before comparing snapshots.")
    if "key_dtype_changes" in risk_types:
        actions.append("Align key column dtypes on old_df and new_df before comparing exact keys.")
    # anchor() workflow hints
    actions.append(
        "MUST: Run anchor('duplicate', df, ['key_cols']) to inspect duplicate keys before dedup."
    )
    actions.append("SKILL: Load skills/data-reconciliation/SKILL.md for structured data comparison.")
    return actions or ["Resolve blocking risks, then rerun diff_tables_by_key."]


def _next_actions(
    added_key_count: int,
    removed_key_count: int,
    changed_key_count: int,
    changed_column_counts: list[dict[str, Any]],
    old_only_columns: list[str],
    new_only_columns: list[str],
    compare_dtype_changes: list[dict[str, str]],
) -> list[str]:
    """Generate next-action suggestions for ok state."""
    actions: list[str] = []
    if changed_key_count and changed_column_counts:
        top_columns = ", ".join(item["column"] for item in changed_column_counts[:5])
        actions.append(f"Review the highest-change columns first: {top_columns}.")
    elif changed_key_count:
        actions.append("Review changed_rows samples to confirm the value changes are expected.")

    if added_key_count or removed_key_count:
        actions.append("Confirm whether added and removed keys are expected for this pipeline run or table version.")

    if old_only_columns or new_only_columns:
        actions.append(
            "Review schema drift separately; one-sided columns are excluded "
            "from row-level value comparison."
        )

    if compare_dtype_changes:
        actions.append("Validate dtype changes before treating value differences as business changes.")

    if not actions:
        actions.append("No row-level changes detected for the compared keys and columns.")

    # anchor() workflow hints
    if changed_key_count:
        actions.append(
            "MUST: Run anchor('quality', new_df, subject='...', keys=[...]) before writing "
            "the updated dataset."
        )
    if old_only_columns or new_only_columns:
        actions.append(
            "MUST: Run anchor('schema_diff', old_df, new_df) for detailed column-level analysis."
        )
    # ── Graph wiring (audit fix) ──

    actions.append("MUST: Run anchor(\"schema_diff\", old_df, new_df) for column-level changes.")

    actions.append("MUST: Run anchor(\"quality\", df, keys=[...]) to validate the newer DataFrame.")

    actions.append("COULD: Run anchor(\"save\", entry_type=\"discovery\") for notable differences.")

    actions.append("SKILL: Load skills/data-reconciliation/SKILL.md for structured data comparison.")

    return actions


def _classify_change_pattern(
    added_count: int,
    removed_count: int,
    changed_count: int,
    unchanged_count: int,
    total_old: int,
    total_new: int,
    changed_column_counts: dict[str, dict[str, int]] | None = None,
) -> dict[str, str]:
    """Classify the overall change pattern between two table versions.

    Returns dict with 'pattern' and 'interpretation'.
    """
    total_keys = max(added_count + removed_count + changed_count + unchanged_count, 1)
    change_rate = (added_count + removed_count + changed_count) / total_keys

    if change_rate < 0.01:
        pattern = "minimal_drift"
        interp = "Nearly identical — less than 1% of keys changed."
    elif added_count > 0 and removed_count == 0 and changed_count == 0:
        pattern = "pure_append"
        interp = f"Pure append — {added_count:,} new rows, no modifications or removals."
    elif added_count > changed_count and removed_count < added_count * 0.1:
        pattern = "incremental_load"
        interp = f"Looks like an incremental load — mostly new rows ({added_count:,} added) with minor updates ({changed_count:,})."
    elif removed_count > total_old * 0.5 and added_count > total_new * 0.5:
        pattern = "full_replacement"
        interp = "Looks like a full replacement or backfill — majority of keys changed on both sides."
    elif changed_count > added_count and changed_count > removed_count:
        pattern = "in_place_update"
        interp = f"Primarily in-place updates — {changed_count:,} rows modified, structure stable."
    elif removed_count > added_count * 2:
        pattern = "data_loss"
        interp = f"Significant data loss — {removed_count:,} rows removed vs {added_count:,} added. Investigate whether this is intentional."
    else:
        pattern = "mixed_change"
        interp = f"Mixed changes: {added_count:,} added, {removed_count:,} removed, {changed_count:,} modified."

    # Add column-level interpretation if available
    col_interp = ""
    if changed_column_counts:
        null_to_value = sum(c.get("null_to_value", 0) for c in changed_column_counts.values())
        value_to_null = sum(c.get("value_to_null", 0) for c in changed_column_counts.values())
        value_changed = sum(c.get("value_changed", 0) for c in changed_column_counts.values())

        if null_to_value > value_changed and null_to_value > value_to_null:
            col_interp = " Column changes are mostly null→value — suggests backfill or data completion."
        elif value_to_null > value_changed and value_to_null > null_to_value:
            col_interp = " Column changes are mostly value→null — possible regression or field loss."
        elif value_changed > null_to_value + value_to_null:
            col_interp = " Column changes are mostly value→value — genuine business data updates."

    return {"pattern": pattern, "interpretation": interp + col_interp}


def _build_merge_expr(
    keys: list[str],
    compare_cols: list[str],
    subject: str,
    engine: str,
) -> str:
    """Generate a ready-to-paste MERGE INTO statement.

    Produces SQL MERGE for Spark engine, and a odibi_anchor
    merge_into() call as a Python-style alternative.
    """
    if not keys or not compare_cols:
        return ""

    # Build SQL MERGE INTO
    table_name = subject if subject else "target_table"
    # Sanitize table_name for SQL
    table_name = table_name.replace(" ", "_").replace(
        ":", "_"
    )

    on_clause = " AND ".join(
        f"target.{k} = source.{k}" for k in keys
    )
    update_set = ", ".join(
        f"target.{c} = source.{c}" for c in compare_cols
    )
    insert_cols = ", ".join(keys + compare_cols)
    insert_vals = ", ".join(
        f"source.{c}" for c in keys + compare_cols
    )

    merge_sql = (
        f"MERGE INTO {{target_table}} AS target\n"
        f"USING {{source_table}} AS source\n"
        f"ON {on_clause}\n"
        f"WHEN MATCHED THEN UPDATE SET {update_set}\n"
        f"WHEN NOT MATCHED THEN INSERT "
        f"({insert_cols}) "
        f"VALUES ({insert_vals})\n"
        f"WHEN NOT MATCHED BY SOURCE THEN DELETE"
    )

    return merge_sql


def _build_context(
    *,
    subject: str,
    engine: str,
    status: str,
    summary: str,
    metrics: dict[str, Any],
    columns: dict[str, Any],
    findings: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    samples: dict[str, list[dict[str, Any]]],
    suggested_next_actions: list[str],
    merge_expr: str = "",
) -> dict[str, Any]:
    """Assemble the final context dict from all components."""
    result = {
        "kind": "diff_tables_by_key",
        "subject": subject,
        "engine": engine,
        "status": status,
        "summary": summary,
        "metrics": metrics,
        "columns": columns,
        "findings": findings,
        "risks": risks,
        "samples": samples,
        "suggested_next_actions": suggested_next_actions,
    }
    if merge_expr:
        result["merge_expr"] = merge_expr
    return result


def _diff_tables_by_key_spark(
    old_df: Any,
    new_df: Any,
    keys: list[str],
    compare_columns: list[str] | None,
    subject: str | None,
    sample_limit: int,
) -> dict[str, Any]:
    """Spark backend for diff_tables_by_key.

    Uses full outer join with presence flags, eqNullSafe for
    column comparison, aggregate counts (no full collect), and
    capped .limit(sample_limit).collect() for samples only.
    """
    from functools import reduce
    import operator
    from pyspark.sql import functions as F

    subject_label = subject or "old_df vs new_df"
    old_columns = [f.name for f in old_df.schema.fields]
    new_columns = [f.name for f in new_df.schema.fields]
    _validate_no_duplicate_dataframe_columns("old_df", old_columns)
    _validate_no_duplicate_dataframe_columns("new_df", new_columns)

    compare_cols = _resolve_compare_columns(
        old_columns=old_columns,
        new_columns=new_columns,
        keys=keys,
        compare_columns=compare_columns,
    )

    old_only_columns = [c for c in old_columns if c not in new_columns]
    new_only_columns = [c for c in new_columns if c not in old_columns]
    common_columns = [c for c in old_columns if c in new_columns]

    # Dtype changes via schema comparison
    key_dtype_changes = _dtype_changes_spark(
        old_df, new_df, keys
    )
    compare_dtype_changes = _dtype_changes_spark(
        old_df, new_df, compare_cols
    )
    normalized_key_dtype_changes = _normalize_dtype_changes(
        key_dtype_changes, is_key=True, is_compared=False
    )
    normalized_compare_dtype_changes = _normalize_dtype_changes(
        compare_dtype_changes, is_key=False, is_compared=True
    )
    type_changed_columns = (
        normalized_key_dtype_changes + normalized_compare_dtype_changes
    )
    columns_context = _columns_context(
        keys=keys,
        compare_cols=compare_cols,
        old_only_columns=old_only_columns,
        new_only_columns=new_only_columns,
        common_columns=common_columns,
        type_changed_columns=type_changed_columns,
    )

    # Blocking checks: duplicates, null keys, key dtype changes
    old_duplicates = _duplicate_key_summary_spark(
        old_df, keys, sample_limit
    )
    new_duplicates = _duplicate_key_summary_spark(
        new_df, keys, sample_limit
    )
    old_null_keys = _null_key_summary_spark(
        old_df, keys, sample_limit
    )
    new_null_keys = _null_key_summary_spark(
        new_df, keys, sample_limit
    )

    old_row_count = old_df.count()
    new_row_count = new_df.count()

    base_metrics = {
        "old_row_count": old_row_count,
        "new_row_count": new_row_count,
        "row_count_delta": new_row_count - old_row_count,
        "old_key_count": int(
            old_df.select(keys).dropDuplicates().count()
        ),
        "new_key_count": int(
            new_df.select(keys).dropDuplicates().count()
        ),
        "common_key_count": None,
        "added_key_count": None,
        "removed_key_count": None,
        "changed_key_count": None,
        "unchanged_key_count": None,
        "has_changes": None,
        "compared_column_count": len(compare_cols),
        "common_column_count": len(common_columns),
        "old_only_column_count": len(old_only_columns),
        "new_only_column_count": len(new_only_columns),
        "key_dtype_change_count": len(key_dtype_changes),
        "compare_dtype_change_count": len(compare_dtype_changes),
        "type_changed_column_count": len(type_changed_columns),
        "old_duplicate_key_count": old_duplicates[
            "duplicate_key_count"
        ],
        "old_duplicate_row_count": old_duplicates[
            "duplicate_row_count"
        ],
        "new_duplicate_key_count": new_duplicates[
            "duplicate_key_count"
        ],
        "new_duplicate_row_count": new_duplicates[
            "duplicate_row_count"
        ],
        "old_null_key_row_count": old_null_keys[
            "null_key_row_count"
        ],
        "new_null_key_row_count": new_null_keys[
            "null_key_row_count"
        ],
        "changed_column_counts": [],
        "changed_cell_count": None,
    }

    schema_findings = _schema_findings(
        old_only_columns=old_only_columns,
        new_only_columns=new_only_columns,
        compare_dtype_changes=compare_dtype_changes,
        normalized_compare_dtype_changes=(
            normalized_compare_dtype_changes
        ),
        type_changed_columns=type_changed_columns,
    )
    blocking_risks = _blocking_diff_risks(
        old_duplicates=old_duplicates,
        new_duplicates=new_duplicates,
        old_null_keys=old_null_keys,
        new_null_keys=new_null_keys,
        normalized_key_dtype_changes=normalized_key_dtype_changes,
    )

    if blocking_risks:
        samples = _empty_diff_samples()
        samples.update({
            "old_duplicate_keys": old_duplicates["samples"],
            "new_duplicate_keys": new_duplicates["samples"],
            "old_null_key_rows": old_null_keys["samples"],
            "new_null_key_rows": new_null_keys["samples"],
        })
        return _build_context(
            subject=subject_label,
            engine="spark",
            status="blocked",
            summary=_blocked_summary(
                subject_label, blocking_risks
            ),
            metrics=base_metrics,
            columns=columns_context,
            findings=schema_findings,
            risks=blocking_risks,
            samples=samples,
            suggested_next_actions=_blocked_next_actions(
                blocking_risks
            ),
        )

    # Full outer join with prefixed compare columns
    old_selected = old_df.select(
        *[F.col(k) for k in keys],
        *[F.col(c).alias(f"old__{c}") for c in compare_cols],
    ).withColumn("__in_old", F.lit(True))

    new_selected = new_df.select(
        *[F.col(k) for k in keys],
        *[F.col(c).alias(f"new__{c}") for c in compare_cols],
    ).withColumn("__in_new", F.lit(True))

    joined = old_selected.join(
        new_selected, on=keys, how="full_outer"
    )

    # Row classification expressions
    is_added = (
        F.col("__in_old").isNull()
        & F.col("__in_new").isNotNull()
    )
    is_removed = (
        F.col("__in_old").isNotNull()
        & F.col("__in_new").isNull()
    )
    is_common = (
        F.col("__in_old").isNotNull()
        & F.col("__in_new").isNotNull()
    )

    # Null-safe change detection per compare column
    change_exprs = {
        c: ~F.col(f"old__{c}").eqNullSafe(F.col(f"new__{c}"))
        for c in compare_cols
    }

    # Any-column-changed expression
    if change_exprs:
        any_changed = reduce(
            operator.or_, change_exprs.values()
        )
    else:
        any_changed = F.lit(False)

    is_changed = is_common & any_changed
    is_unchanged = is_common & ~any_changed

    # Aggregate counts in a SINGLE Spark action
    count_exprs = [
        F.sum(F.when(is_added, 1).otherwise(0)).alias(
            "added"
        ),
        F.sum(F.when(is_removed, 1).otherwise(0)).alias(
            "removed"
        ),
        F.sum(F.when(is_common, 1).otherwise(0)).alias(
            "common"
        ),
        F.sum(F.when(is_changed, 1).otherwise(0)).alias(
            "changed"
        ),
        F.sum(F.when(is_unchanged, 1).otherwise(0)).alias(
            "unchanged"
        ),
    ]
    # Per-column change counts
    for c in compare_cols:
        count_exprs.append(
            F.sum(
                F.when(is_common & change_exprs[c], 1)
                .otherwise(0)
            ).alias(f"cc__{c}")
        )

    agg_row = joined.select(*count_exprs).collect()[0]

    added_key_count = int(agg_row["added"] or 0)
    removed_key_count = int(agg_row["removed"] or 0)
    common_key_count = int(agg_row["common"] or 0)
    changed_key_count = int(agg_row["changed"] or 0)
    unchanged_key_count = int(agg_row["unchanged"] or 0)

    changed_column_counts = sorted(
        [
            {
                "column": c,
                "changed_key_count": int(
                    agg_row[f"cc__{c}"] or 0
                ),
            }
            for c in compare_cols
            if int(agg_row[f"cc__{c}"] or 0) > 0
        ],
        key=lambda x: (
            -x["changed_key_count"], x["column"]
        ),
    )
    changed_cell_count = sum(
        item["changed_key_count"]
        for item in changed_column_counts
    )

    has_changes = bool(
        added_key_count
        or removed_key_count
        or changed_key_count
        or old_only_columns
        or new_only_columns
        or key_dtype_changes
        or compare_dtype_changes
    )

    # Percentage metrics (v2)
    _old_kc = int(base_metrics["old_key_count"]) or 1
    added_key_pct = round(added_key_count / _old_kc, 4)
    removed_key_pct = round(
        removed_key_count / _old_kc, 4
    )
    changed_key_pct = round(
        changed_key_count / _old_kc, 4
    )

    metrics = dict(base_metrics)
    metrics.update({
        "common_key_count": common_key_count,
        "added_key_count": added_key_count,
        "removed_key_count": removed_key_count,
        "changed_key_count": changed_key_count,
        "unchanged_key_count": unchanged_key_count,
        "has_changes": has_changes,
        "changed_column_counts": changed_column_counts,
        "changed_cell_count": changed_cell_count,
        "added_key_pct": added_key_pct,
        "removed_key_pct": removed_key_pct,
        "changed_key_pct": changed_key_pct,
    })

    # Collect capped samples only
    samples = _empty_diff_samples()
    samples.update({
        "added_keys": _sample_keys_spark(
            joined, is_added, keys, sample_limit
        ),
        "removed_keys": _sample_keys_spark(
            joined, is_removed, keys, sample_limit
        ),
        "added_rows": _sample_side_rows_spark(
            joined, is_added, keys, compare_cols,
            "new", sample_limit
        ),
        "removed_rows": _sample_side_rows_spark(
            joined, is_removed, keys, compare_cols,
            "old", sample_limit
        ),
        "changed_rows": _sample_changed_rows_spark(
            joined, is_changed, change_exprs, keys,
            compare_cols, sample_limit
        ),
    })

    findings = (
        _change_findings(
            added_key_count=added_key_count,
            removed_key_count=removed_key_count,
            changed_key_count=changed_key_count,
            unchanged_key_count=unchanged_key_count,
            changed_column_counts=changed_column_counts,
            compare_column_count=len(compare_cols),
        )
        + schema_findings
    )
    risks = _non_blocking_diff_risks(
        removed_key_count=removed_key_count,
        old_only_columns=old_only_columns,
        new_only_columns=new_only_columns,
        compare_dtype_changes=compare_dtype_changes,
        normalized_compare_dtype_changes=(
            normalized_compare_dtype_changes
        ),
        compared_column_count=len(compare_cols),
        common_key_count=common_key_count,
        old_key_count=base_metrics["old_key_count"],
        new_key_count=base_metrics["new_key_count"],
    )

    merge_expr = _build_merge_expr(
        keys=keys,
        compare_cols=compare_cols,
        subject=subject_label,
        engine="spark",
    )

    cc_dict = {item["column"]: item for item in changed_column_counts}
    change_pattern = _classify_change_pattern(
        added_count=added_key_count,
        removed_count=removed_key_count,
        changed_count=changed_key_count,
        unchanged_count=unchanged_key_count,
        total_old=int(base_metrics["old_key_count"]),
        total_new=int(base_metrics["new_key_count"]),
        changed_column_counts=cc_dict if cc_dict else None,
    )

    ctx = _build_context(
        subject=subject_label,
        engine="spark",
        status="ok",
        merge_expr=merge_expr,
        summary=_ok_summary(
            subject_label=subject_label,
            keys=keys,
            old_key_count=base_metrics["old_key_count"],
            new_key_count=base_metrics["new_key_count"],
            added_key_count=added_key_count,
            removed_key_count=removed_key_count,
            changed_key_count=changed_key_count,
        ),
        metrics=metrics,
        columns=columns_context,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=_next_actions(
            added_key_count=added_key_count,
            removed_key_count=removed_key_count,
            changed_key_count=changed_key_count,
            changed_column_counts=changed_column_counts,
            old_only_columns=old_only_columns,
            new_only_columns=new_only_columns,
            compare_dtype_changes=compare_dtype_changes,
        ),
    )
    ctx["change_pattern"] = change_pattern
    return ctx


# -------------------------------------------------------------------
# Spark helpers
# -------------------------------------------------------------------


def _dtype_changes_spark(
    old_df: Any, new_df: Any, columns: list[str]
) -> list[dict[str, str]]:
    """Detect dtype differences between Spark schemas."""
    old_types = {
        f.name: str(f.dataType) for f in old_df.schema.fields
    }
    new_types = {
        f.name: str(f.dataType) for f in new_df.schema.fields
    }
    changes = []
    for col in columns:
        if col not in old_types or col not in new_types:
            continue
        if old_types[col] != new_types[col]:
            changes.append({
                "column": col,
                "old_dtype": old_types[col],
                "new_dtype": new_types[col],
            })
    return changes


def _duplicate_key_summary_spark(
    df: Any, keys: list[str], sample_limit: int
) -> dict[str, Any]:
    """Detect duplicate keys in a Spark DataFrame."""
    from pyspark.sql import functions as F

    if df.head(1) == []:
        return {
            "duplicate_key_count": 0,
            "duplicate_row_count": 0,
            "samples": [],
        }

    grouped = df.groupBy(keys).agg(
        F.count("*").alias("row_count")
    )
    duplicates = grouped.where(F.col("row_count") > 1)
    dup_count_row = duplicates.agg(
        F.count("*").alias("key_count"),
        F.sum("row_count").alias("row_count"),
    ).collect()[0]

    dup_key_count = int(dup_count_row["key_count"] or 0)
    dup_row_count = int(dup_count_row["row_count"] or 0)

    samples = []
    if dup_key_count > 0 and sample_limit > 0:
        sample_rows = (
            duplicates.limit(sample_limit).collect()
        )
        samples = _records_to_json_safe(
            [row.asDict() for row in sample_rows]
        )

    return {
        "duplicate_key_count": dup_key_count,
        "duplicate_row_count": dup_row_count,
        "samples": samples,
    }


def _null_key_summary_spark(
    df: Any, keys: list[str], sample_limit: int
) -> dict[str, Any]:
    """Detect null key values in a Spark DataFrame."""
    from functools import reduce
    import operator
    from pyspark.sql import functions as F

    if df.head(1) == []:
        return {"null_key_row_count": 0, "samples": []}

    null_expr = reduce(
        operator.or_,
        [F.col(k).isNull() for k in keys],
    )
    null_count = df.where(null_expr).count()

    samples = []
    if null_count > 0 and sample_limit > 0:
        sample_rows = (
            df.where(null_expr)
            .select(keys)
            .limit(sample_limit)
            .collect()
        )
        samples = _records_to_json_safe(
            [row.asDict() for row in sample_rows]
        )

    return {
        "null_key_row_count": int(null_count),
        "samples": samples,
    }


def _sample_keys_spark(
    joined: Any,
    condition: Any,
    keys: list[str],
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Collect capped key-only sample from joined frame."""
    if sample_limit == 0:
        return []
    rows = (
        joined.where(condition)
        .select(keys)
        .limit(sample_limit)
        .collect()
    )
    return _records_to_json_safe(
        [row.asDict() for row in rows]
    )


def _sample_side_rows_spark(
    joined: Any,
    condition: Any,
    keys: list[str],
    compare_cols: list[str],
    side: str,
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Collect capped full-row samples from one side."""
    if sample_limit == 0:
        return []
    # Select keys + prefixed compare columns for the side
    select_cols = list(keys) + [
        f"{side}__{c}" for c in compare_cols
    ]
    rows = (
        joined.where(condition)
        .select(select_cols)
        .limit(sample_limit)
        .collect()
    )
    # Rename back: strip the side prefix
    result = []
    for row in rows:
        record = {}
        for k in keys:
            record[k] = _json_safe_value(row[k])
        for c in compare_cols:
            record[c] = _json_safe_value(
                row[f"{side}__{c}"]
            )
        result.append(record)
    return result


def _sample_changed_rows_spark(
    joined: Any,
    is_changed: Any,
    change_exprs: dict[str, Any],
    keys: list[str],
    compare_cols: list[str],
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Collect capped changed-row samples with diffs."""
    if sample_limit == 0:
        return []

    # Select keys + old/new columns for changed rows
    select_cols = list(keys)
    for c in compare_cols:
        select_cols.append(f"old__{c}")
        select_cols.append(f"new__{c}")

    rows = (
        joined.where(is_changed)
        .select(select_cols)
        .limit(sample_limit)
        .collect()
    )

    result = []
    for row in rows:
        row_dict = row.asDict()
        # Determine which columns changed for this row
        changed_columns = []
        for c in compare_cols:
            old_val = row_dict.get(f"old__{c}")
            new_val = row_dict.get(f"new__{c}")
            # Null-safe comparison
            if old_val != new_val:
                if not (old_val is None and new_val is None):
                    changed_columns.append(c)

        shown_columns = changed_columns[:sample_limit]
        changes = {
            c: {
                "old": _json_safe_value(
                    row_dict[f"old__{c}"]
                ),
                "new": _json_safe_value(
                    row_dict[f"new__{c}"]
                ),
            }
            for c in shown_columns
        }
        result.append({
            "key": {
                k: _json_safe_value(row_dict[k])
                for k in keys
            },
            "changed_column_count": len(changed_columns),
            "changed_columns": shown_columns,
            "changes": changes,
            "truncated_changed_columns": (
                len(changed_columns) > len(shown_columns)
            ),
        })
    return result
