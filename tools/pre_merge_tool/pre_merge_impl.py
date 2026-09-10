"""Pre-merge validation tool implementation.

Entry point: pre_merge_context(source_df, target, keys=[...], ...)
Validates a source DataFrame is safe to MERGE INTO a Delta target.
Returns a standard Anchor contract with findings, risks, and suggested_next_actions.
"""
from __future__ import annotations

from typing import Any


# ============================================================================
# Engine detection (inlined — no imports from odibi_anchor src)
# ============================================================================

def _detect_engine(df: Any) -> str:
    """Detect Pandas vs Spark safely. Returns 'pandas' | 'spark' | 'unknown'."""
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            return "pandas"
    except Exception:
        pass
    try:
        from pyspark.sql import DataFrame as SparkDF
        if isinstance(df, SparkDF):
            return "spark"
    except Exception:
        pass
    mod = getattr(type(df), "__module__", "") or ""
    if "pyspark.sql" in mod:
        return "spark"
    if "pandas" in mod:
        return "pandas"
    return "unknown"


# ============================================================================
# Type compatibility matrix
# ============================================================================

# Maps (source_type_normalized, target_type_normalized) → (compatible: bool, risk: str, note: str)
# "compatible" means the MERGE will not fail on type alone.
# Normalized types are lowercase base types stripped of parameters.

_TYPE_COMPAT: dict[tuple[str, str], tuple[bool, str, str]] = {
    # Safe upcasts
    ("int", "long"): (True, "none", "implicit upcast"),
    ("int", "bigint"): (True, "none", "implicit upcast"),
    ("short", "int"): (True, "none", "implicit upcast"),
    ("short", "long"): (True, "none", "implicit upcast"),
    ("short", "bigint"): (True, "none", "implicit upcast"),
    ("byte", "short"): (True, "none", "implicit upcast"),
    ("byte", "int"): (True, "none", "implicit upcast"),
    ("byte", "long"): (True, "none", "implicit upcast"),
    ("int", "double"): (True, "none", "implicit upcast"),
    ("int", "float"): (True, "low", "possible precision loss for large ints"),
    ("long", "double"): (True, "low", "possible precision loss for large longs"),
    ("float", "double"): (True, "none", "implicit upcast"),
    # Dangerous casts — MERGE will fail
    ("string", "int"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "long"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "bigint"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "short"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "byte"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "float"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "double"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "decimal"): (False, "high", "MERGE will fail on non-numeric values"),
    ("string", "boolean"): (False, "high", "MERGE will fail on non-boolean string values"),
    # Risky but may work
    ("double", "decimal"): (True, "medium", "precision loss possible"),
    ("float", "decimal"): (True, "medium", "precision loss possible"),
    ("string", "date"): (False, "high", "depends on format consistency — likely to fail"),
    ("string", "timestamp"): (False, "high", "depends on format consistency — likely to fail"),
    ("date", "timestamp"): (True, "none", "safe upcast with midnight time"),
    ("timestamp", "date"): (True, "low", "time component truncated"),
    # Same type
    ("string", "string"): (True, "none", "same type"),
    ("int", "int"): (True, "none", "same type"),
    ("long", "long"): (True, "none", "same type"),
    ("bigint", "bigint"): (True, "none", "same type"),
    ("double", "double"): (True, "none", "same type"),
    ("float", "float"): (True, "none", "same type"),
    ("boolean", "boolean"): (True, "none", "same type"),
    ("date", "date"): (True, "none", "same type"),
    ("timestamp", "timestamp"): (True, "none", "same type"),
    ("decimal", "decimal"): (True, "none", "same type"),
    ("binary", "binary"): (True, "none", "same type"),
    ("short", "short"): (True, "none", "same type"),
    ("byte", "byte"): (True, "none", "same type"),
}


def _normalize_type(type_str: str) -> str:
    """Normalize a type string to its base form for compat lookup.
    
    'decimal(10,2)' -> 'decimal', 'varchar(100)' -> 'string', 'integer' -> 'int'
    """
    t = str(type_str).lower().strip()
    # Strip parameters
    if "(" in t:
        t = t[:t.index("(")]
    # Normalize aliases
    aliases = {
        "integer": "int",
        "bigint": "long",
        "tinyint": "byte",
        "smallint": "short",
        "real": "float",
        "varchar": "string",
        "char": "string",
        "text": "string",
        "datetime": "timestamp",
        "int64": "long",
        "int32": "int",
        "float64": "double",
        "float32": "float",
        "object": "string",
        "bool": "boolean",
        "datetime64[ns]": "timestamp",
        "datetime64": "timestamp",
    }
    return aliases.get(t, t)


def _check_type_compatibility(source_type: str, target_type: str) -> dict:
    """Check if source_type can be safely written to target_type in a MERGE."""
    src_norm = _normalize_type(source_type)
    tgt_norm = _normalize_type(target_type)
    
    if src_norm == tgt_norm:
        return {"compatible": True, "risk": "none", "note": "same type"}
    
    key = (src_norm, tgt_norm)
    if key in _TYPE_COMPAT:
        compat, risk, note = _TYPE_COMPAT[key]
        return {"compatible": compat, "risk": risk, "note": note}
    
    # Reverse direction check (target → source) for downcasts
    rev_key = (tgt_norm, src_norm)
    if rev_key in _TYPE_COMPAT:
        compat, _, _ = _TYPE_COMPAT[rev_key]
        if compat:
            # Reverse of safe upcast = risky downcast
            return {"compatible": True, "risk": "medium", "note": f"downcast from {src_norm} to {tgt_norm} — possible data loss"}
        else:
            return {"compatible": False, "risk": "high", "note": f"incompatible cast from {src_norm} to {tgt_norm}"}
    
    # Unknown pair — warn rather than block (conservative approach)
    return {"compatible": True, "risk": "medium", "note": f"unknown type pair ({src_norm} → {tgt_norm}) — verify manually"}


# ============================================================================
# Pandas helpers
# ============================================================================

def _get_schema_pandas(df) -> dict[str, str]:
    """Get column → type mapping for a pandas DataFrame."""
    return {col: str(dtype) for col, dtype in df.dtypes.items()}


def _count_pandas(df) -> int:
    return len(df)


def _duplicate_keys_pandas(df, keys: list[str], sample_limit: int) -> dict:
    """Find duplicate keys in a pandas DataFrame."""
    import pandas as pd
    grouped = df.groupby(keys, dropna=False).size().reset_index(name="_count")
    dupes = grouped[grouped["_count"] > 1]
    dupe_count = len(dupes)
    
    if dupe_count == 0:
        return {"count": 0, "samples": [], "total_excess_rows": 0}
    
    top_dupes = dupes.nlargest(sample_limit, "_count")
    samples = []
    for _, row in top_dupes.iterrows():
        sample = {k: row[k] for k in keys}
        sample["_duplicate_count"] = int(row["_count"])
        samples.append(sample)
    
    total_excess = int(dupes["_count"].sum() - dupe_count)  # excess rows beyond 1 per key
    return {"count": dupe_count, "samples": samples, "total_excess_rows": total_excess}


def _null_keys_pandas(df, keys: list[str]) -> dict:
    """Check for null values in key columns (pandas)."""
    total = len(df)
    per_column = {}
    any_null_mask = df[keys].isna().any(axis=1)
    rows_with_any_null = int(any_null_mask.sum())
    
    for col in keys:
        null_count = int(df[col].isna().sum())
        per_column[col] = {
            "null_count": null_count,
            "null_pct": round(null_count / total, 6) if total > 0 else 0.0,
        }
    
    return {
        "total_null_key_rows": rows_with_any_null,
        "null_pct": round(rows_with_any_null / total, 6) if total > 0 else 0.0,
        "per_column": per_column,
    }


def _key_overlap_pandas(source_df, target_df, keys: list[str]) -> dict:
    """Compute key overlap between source and target (pandas)."""
    import pandas as pd
    
    # Get distinct keys from each side
    source_keys = source_df[keys].drop_duplicates()
    target_keys = target_df[keys].drop_duplicates()
    
    source_distinct = len(source_keys)
    target_distinct = len(target_keys)
    
    # Merge to find overlap
    merged = source_keys.merge(target_keys, on=keys, how="inner")
    overlap_count = len(merged)
    
    insert_count = source_distinct - overlap_count
    update_count = overlap_count
    
    if insert_count == 0 and update_count > 0:
        behavior = "UPDATE-only"
    elif update_count == 0 and insert_count > 0:
        behavior = "INSERT-only"
    elif insert_count == 0 and update_count == 0:
        behavior = "EMPTY"
    else:
        behavior = "MIXED"
    
    return {
        "source_key_distinct": source_distinct,
        "target_key_distinct": target_distinct,
        "overlap_count": overlap_count,
        "insert_count": insert_count,
        "update_count": update_count,
        "merge_behavior": behavior,
    }


# ============================================================================
# Spark helpers
# ============================================================================

def _get_schema_spark(df) -> dict[str, str]:
    """Get column → type mapping for a Spark DataFrame."""
    return {f.name: f.dataType.simpleString() for f in df.schema.fields}


def _count_spark(df) -> int:
    return df.count()


def _duplicate_keys_spark(df, keys: list[str], sample_limit: int) -> dict:
    """Find duplicate keys in a Spark DataFrame."""
    from pyspark.sql import functions as F
    
    grouped = df.groupBy(keys).agg(F.count("*").alias("_count"))
    dupes = grouped.where("_count > 1")
    dupe_count = dupes.count()
    
    if dupe_count == 0:
        return {"count": 0, "samples": [], "total_excess_rows": 0}
    
    top_dupes = dupes.orderBy(F.col("_count").desc()).limit(sample_limit)
    rows = top_dupes.collect()
    samples = []
    for row in rows:
        sample = {k: row[k] for k in keys}
        sample["_duplicate_count"] = int(row["_count"])
        samples.append(sample)
    
    total_excess = int(dupes.agg(F.sum("_count")).first()[0]) - dupe_count
    return {"count": dupe_count, "samples": samples, "total_excess_rows": total_excess}


def _null_keys_spark(df, keys: list[str]) -> dict:
    """Check for null values in key columns (Spark)."""
    from pyspark.sql import functions as F
    
    total = df.count()
    
    # Per-column null counts (single pass)
    null_exprs = [F.sum(F.when(F.col(c).isNull(), 1).otherwise(0)).alias(c) for c in keys]
    row = df.select(*null_exprs).first()
    
    per_column = {}
    for col in keys:
        null_count = int(row[col]) if row[col] is not None else 0
        per_column[col] = {
            "null_count": null_count,
            "null_pct": round(null_count / total, 6) if total > 0 else 0.0,
        }
    
    # Any-null row count
    any_null_cond = F.lit(False)
    for c in keys:
        any_null_cond = any_null_cond | F.col(c).isNull()
    rows_with_any_null = df.where(any_null_cond).count()
    
    return {
        "total_null_key_rows": rows_with_any_null,
        "null_pct": round(rows_with_any_null / total, 6) if total > 0 else 0.0,
        "per_column": per_column,
    }


def _key_overlap_spark(source_df, target_df, keys: list[str]) -> dict:
    """Compute key overlap between source and target (Spark)."""
    source_keys = source_df.select(keys).distinct()
    target_keys = target_df.select(keys).distinct()
    
    source_distinct = source_keys.count()
    target_distinct = target_keys.count()
    
    overlap_count = source_keys.intersect(target_keys).count()
    
    insert_count = source_distinct - overlap_count
    update_count = overlap_count
    
    if insert_count == 0 and update_count > 0:
        behavior = "UPDATE-only"
    elif update_count == 0 and insert_count > 0:
        behavior = "INSERT-only"
    elif insert_count == 0 and update_count == 0:
        behavior = "EMPTY"
    else:
        behavior = "MIXED"
    
    return {
        "source_key_distinct": source_distinct,
        "target_key_distinct": target_distinct,
        "overlap_count": overlap_count,
        "insert_count": insert_count,
        "update_count": update_count,
        "merge_behavior": behavior,
    }


# ============================================================================
# Schema comparison
# ============================================================================

def _compare_schemas(
    source_schema: dict[str, str],
    target_schema: dict[str, str],
) -> dict:
    """Compare source and target schemas for MERGE compatibility.
    
    Returns dict with columns_added, columns_removed, type_mismatches,
    and the full compatibility matrix.
    """
    source_cols = set(source_schema.keys())
    target_cols = set(target_schema.keys())
    
    added = sorted(source_cols - target_cols)  # in source but not target
    removed = sorted(target_cols - source_cols)  # in target but not source
    common = sorted(source_cols & target_cols)
    
    type_mismatches = []
    compatibility_matrix = {}
    schema_compatible = True
    
    for col in common:
        src_type = source_schema[col]
        tgt_type = target_schema[col]
        compat = _check_type_compatibility(src_type, tgt_type)
        compatibility_matrix[col] = {
            "source_type": src_type,
            "target_type": tgt_type,
            **compat,
        }
        if not compat["compatible"]:
            schema_compatible = False
            type_mismatches.append({
                "column": col,
                "source_type": src_type,
                "target_type": tgt_type,
                "risk": compat["risk"],
                "note": compat["note"],
            })
    
    return {
        "columns_added": added,
        "columns_removed": removed,
        "columns_type_mismatch": type_mismatches,
        "type_compatibility": compatibility_matrix,
        "schema_compatible": schema_compatible,
    }


# ============================================================================
# Markdown renderer
# ============================================================================

def _render_pre_merge_report(ctx: dict) -> str:
    """Render pre_merge context as markdown report."""
    lines = []
    m = ctx["metrics"]
    
    # Header
    status = "✅" if not ctx["risks"] else "⚠️"
    lines.append(f"# {status} Pre-Merge Validation: {ctx['subject']}")
    lines.append("")
    lines.append(f"**{ctx['summary']}**")
    lines.append("")
    
    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Source rows | {m['source_count']:,} |")
    lines.append(f"| Target rows | {m['target_count']:,} |")
    lines.append(f"| Source distinct keys | {m['source_key_distinct']:,} |")
    lines.append(f"| Duplicate key groups | {m['source_duplicate_key_count']:,} |")
    lines.append(f"| Null key rows | {m['source_null_key_count']:,} ({m['source_null_key_pct']:.1%}) |")
    lines.append(f"| Key overlap | {m['overlap_count']:,} |")
    lines.append(f"| Predicted INSERTs | {m['insert_count']:,} |")
    lines.append(f"| Predicted UPDATEs | {m['update_count']:,} |")
    lines.append(f"| Merge behavior | **{m['merge_behavior']}** |")
    lines.append(f"| Source/Target ratio | {m['source_target_ratio']:.3f} |")
    lines.append(f"| Schema compatible | {'✅' if m['schema_compatible'] else '❌'} |")
    lines.append("")
    
    # Findings
    if ctx["findings"]:
        lines.append("## Findings")
        lines.append("")
        for f in ctx["findings"]:
            lines.append(f"* {f}")
        lines.append("")
    
    # Risks
    if ctx["risks"]:
        lines.append("## ⚠️ Risks")
        lines.append("")
        for r in ctx["risks"]:
            lines.append(f"* {r}")
        lines.append("")
    
    # Samples
    samples = ctx.get("samples", {})
    if samples.get("duplicate_keys"):
        lines.append("## Duplicate Keys (top offenders)")
        lines.append("")
        for s in samples["duplicate_keys"]:
            count = s.pop("_duplicate_count", "?")
            keys_str = ", ".join(f"{k}={v}" for k, v in s.items())
            lines.append(f"* `{keys_str}` — {count} occurrences")
        lines.append("")
    
    if samples.get("type_mismatch_columns"):
        lines.append("## Type Mismatches")
        lines.append("")
        lines.append("| Column | Source | Target | Risk | Note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for tm in samples["type_mismatch_columns"]:
            lines.append(f"| {tm['column']} | {tm['source_type']} | {tm['target_type']} | {tm['risk']} | {tm['note']} |")
        lines.append("")
    
    # Suggested actions
    if ctx["suggested_next_actions"]:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for i, a in enumerate(ctx["suggested_next_actions"], 1):
            lines.append(f"{i}. {a}")
        lines.append("")
    
    return "\n".join(lines)


# ============================================================================
# Public entry point
# ============================================================================

def pre_merge_context(
    *args,
    source_df=None,
    target=None,
    keys: list[str] | None = None,
    source_subject: str = "source",
    target_subject: str = "target",
    output_format: str = "dict",
    sample_limit: int = 10,
    **kwargs,
) -> dict | str:
    """Validate a source DataFrame is safe to MERGE INTO a Delta target.
    
    Parameters
    ----------
    source_df : DataFrame (Spark or Pandas)
        The source data to merge.
    target : str or DataFrame
        Delta table name (e.g., "catalog.schema.table") or a DataFrame.
    keys : list[str]
        Merge key column(s).
    source_subject : str
        Label for the source in output.
    target_subject : str
        Label for the target in output (overridden by table name if string target).
    output_format : str
        "dict" or "markdown".
    sample_limit : int
        Max samples to show for duplicates, etc.
    
    Returns
    -------
    dict or str
        Standard Anchor contract or markdown report.
    """
    # ── Import contract utilities ─────────────────────────────────────────
    import sys
    import os
    # Add src path for contract imports
    _src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src",
    )
    if _src_path not in sys.path:
        sys.path.insert(0, _src_path)
    
    from odibi_anchor._utils.contract import (
        build_base_context,
        finalize_context,
        guard_dataframe_type,
        validate_output_format,
    )
    
    validate_output_format(output_format)
    
    # ── Resolve positional args ───────────────────────────────────────────
    if args:
        if source_df is None and len(args) >= 1:
            source_df = args[0]
        if target is None and len(args) >= 2:
            target = args[1]
        if keys is None and len(args) >= 3:
            keys = args[2]
    
    # ── Validate inputs ───────────────────────────────────────────────────
    if source_df is None:
        raise ValueError(
            "source_df is required — pass the source DataFrame: "
            "anchor('pre_merge', source_df, target, keys=['id']) "
            "— see anchor('help', 'pre_merge')."
        )
    if target is None:
        raise ValueError(
            "target is required — pass a Delta table name (str) or target DataFrame: "
            "anchor('pre_merge', source_df, 'catalog.schema.table', keys=['id']) "
            "— see anchor('help', 'pre_merge')."
        )
    if not keys:
        raise ValueError(
            "keys is required — pass a list of merge key column names: "
            "anchor('pre_merge', source_df, target, keys=['id']) "
            "— see anchor('help', 'pre_merge')."
        )
    if isinstance(keys, str):
        keys = [keys]

    guard_dataframe_type(source_df, "source_df")
    engine = _detect_engine(source_df)
    
    # ── Resolve target ────────────────────────────────────────────────────
    target_is_table = isinstance(target, str)
    if target_is_table:
        target_subject = target
        # Load target via spark.table()
        try:
            from pyspark.sql import SparkSession
            spark = SparkSession.getActiveSession()
            if spark is None:
                raise RuntimeError("No active SparkSession — cannot read target table")
            target_df = spark.table(target)
        except ImportError:
            raise RuntimeError(
                f"Cannot read target table '{target}' — PySpark not available. "
                "Pass a DataFrame as target for testing."
            )
    else:
        target_df = target
        guard_dataframe_type(target_df, "target")
    
    # ── Validate key columns exist ────────────────────────────────────────
    if engine == "pandas":
        source_cols = set(source_df.columns)
        target_cols = set(target_df.columns)
    else:
        source_cols = {f.name for f in source_df.schema.fields}
        target_cols = {f.name for f in target_df.schema.fields}
    
    missing_in_source = [k for k in keys if k not in source_cols]
    missing_in_target = [k for k in keys if k not in target_cols]
    
    if missing_in_source:
        raise ValueError(
            f"Merge key(s) not found in source: {missing_in_source}. "
            "Check keys= against source_df.columns — see anchor('help', 'pre_merge')."
        )
    if missing_in_target:
        raise ValueError(
            f"Merge key(s) not found in target: {missing_in_target}. "
            "Check keys= against target columns — see anchor('help', 'pre_merge')."
        )
    
    # ── Run checks ────────────────────────────────────────────────────────
    findings = []
    risks = []
    
    # Counts
    if engine == "pandas":
        source_count = _count_pandas(source_df)
        target_count = _count_pandas(target_df)
    else:
        source_count = _count_spark(source_df)
        target_count = _count_spark(target_df)
    
    # 1. Duplicate key check
    if engine == "pandas":
        dupe_result = _duplicate_keys_pandas(source_df, keys, sample_limit)
    else:
        dupe_result = _duplicate_keys_spark(source_df, keys, sample_limit)
    
    dupe_count = dupe_result["count"]
    if dupe_count > 0:
        risks.append(
            f"MERGE WILL FAIL: {dupe_count:,} duplicate key group(s) in source — "
            f"Delta MERGE requires unique keys in source when matching target rows. "
            f"Total excess rows: {dupe_result['total_excess_rows']:,}"
        )
    else:
        findings.append("No duplicate keys in source — MERGE ON condition is safe")
    
    # 2. Null key check
    if engine == "pandas":
        null_result = _null_keys_pandas(source_df, keys)
    else:
        null_result = _null_keys_spark(source_df, keys)
    
    null_key_rows = null_result["total_null_key_rows"]
    if null_key_rows > 0:
        risks.append(
            f"{null_key_rows:,} source row(s) have NULL merge keys — "
            f"these will NOT match any target row (NULL != NULL in MERGE ON)"
        )
    else:
        findings.append("No NULL merge keys in source")
    
    # 3. Schema compatibility
    if engine == "pandas":
        source_schema = _get_schema_pandas(source_df)
        target_schema = _get_schema_pandas(target_df)
    else:
        source_schema = _get_schema_spark(source_df)
        target_schema = _get_schema_spark(target_df)
    
    schema_result = _compare_schemas(source_schema, target_schema)
    
    if schema_result["columns_added"]:
        findings.append(
            f"Source has {len(schema_result['columns_added'])} column(s) not in target: "
            f"{schema_result['columns_added'][:5]}"
        )
    if schema_result["columns_removed"]:
        findings.append(
            f"Target has {len(schema_result['columns_removed'])} column(s) not in source: "
            f"{schema_result['columns_removed'][:5]}"
        )
    if schema_result["columns_type_mismatch"]:
        for tm in schema_result["columns_type_mismatch"]:
            risks.append(
                f"Type mismatch on '{tm['column']}': source={tm['source_type']}, "
                f"target={tm['target_type']} — {tm['note']}"
            )
    elif schema_result["schema_compatible"]:
        findings.append("Schema fully compatible — no type conflicts")
    
    # 4. Key overlap / merge behavior prediction
    if engine == "pandas":
        overlap_result = _key_overlap_pandas(source_df, target_df, keys)
    else:
        overlap_result = _key_overlap_spark(source_df, target_df, keys)
    
    merge_behavior = overlap_result["merge_behavior"]
    insert_count = overlap_result["insert_count"]
    update_count = overlap_result["update_count"]
    
    if merge_behavior == "MIXED":
        findings.append(f"{update_count:,} source keys match target — these rows will be UPDATED")
        findings.append(f"{insert_count:,} source keys are new — these rows will be INSERTED")
    elif merge_behavior == "UPDATE-only":
        findings.append(f"All {update_count:,} source keys exist in target — UPDATE-only merge")
    elif merge_behavior == "INSERT-only":
        findings.append(f"All {insert_count:,} source keys are new — INSERT-only merge")
    elif merge_behavior == "EMPTY":
        risks.append("Source has no distinct keys — MERGE will be a no-op")
    
    # 5. Source batch ratio
    source_target_ratio = round(source_count / target_count, 6) if target_count > 0 else float("inf")
    if source_target_ratio > 2.0:
        risks.append(
            f"Source is {source_target_ratio:.1f}x larger than target "
            f"({source_count:,} vs {target_count:,}) — verify this isn't a full reload "
            f"instead of an incremental batch"
        )
    
    # ── Build suggested actions ───────────────────────────────────────────
    suggested = []
    if dupe_count > 0:
        keys_str = ", ".join(f'"{k}"' for k in keys)
        suggested.append(
            f"Deduplicate source before merge: "
            f"source_df.dropDuplicates([{keys_str}])"
        )
    if null_key_rows > 0:
        suggested.append(
            "Filter NULL keys: source_df.where(" +
            " & ".join(f'F.col("{k}").isNotNull()' for k in keys) + ")"
        )
    if schema_result["columns_type_mismatch"]:
        for tm in schema_result["columns_type_mismatch"]:
            col_name = tm["column"]
            tgt_type = tm["target_type"]
            suggested.append(
                f"Fix type for '{col_name}': "
                f'source_df.withColumn("{col_name}", F.col("{col_name}").cast("{tgt_type}"))'
            )
    if not risks:
        suggested.append("Merge looks safe — proceed with MERGE INTO")
    
    # ── Build summary ─────────────────────────────────────────────────────
    status = "BLOCKED" if (dupe_count > 0 or schema_result["columns_type_mismatch"]) else (
        "WARNING" if risks else "OK"
    )
    summary_parts = [f"{status}: MERGE {'unsafe' if status == 'BLOCKED' else 'safe'}"]
    summary_parts.append(f"{dupe_count} duplicate key(s)")
    if not schema_result["schema_compatible"]:
        summary_parts.append("schema incompatible")
    else:
        summary_parts.append("schema compatible")
    summary_parts.append(f"{merge_behavior} merge ({insert_count} inserts, {update_count} updates)")
    summary = " — ".join(summary_parts)
    
    # ── Assemble metrics ──────────────────────────────────────────────────
    metrics = {
        "source_count": source_count,
        "target_count": target_count,
        "source_key_distinct": overlap_result["source_key_distinct"],
        "source_duplicate_key_count": dupe_count,
        "source_null_key_count": null_key_rows,
        "source_null_key_pct": null_result["null_pct"],
        "overlap_count": overlap_result["overlap_count"],
        "insert_count": insert_count,
        "update_count": update_count,
        "merge_behavior": merge_behavior,
        "source_target_ratio": source_target_ratio,
        "schema_compatible": schema_result["schema_compatible"],
        "columns_added": schema_result["columns_added"],
        "columns_removed": schema_result["columns_removed"],
        "columns_type_mismatch": schema_result["columns_type_mismatch"],
        "type_compatibility": schema_result["type_compatibility"],
    }
    
    # ── Samples ───────────────────────────────────────────────────────────
    samples = {
        "duplicate_keys": dupe_result["samples"],
        "null_key_rows": [],  # Could add sampling of null-key rows if needed
        "type_mismatch_columns": schema_result["columns_type_mismatch"],
    }
    
    # ── Build and finalize context ────────────────────────────────────────
    subject = f"{source_subject} → {target_subject}"
    ctx = build_base_context(
        kind="pre_merge",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=suggested,
    )
    
    return finalize_context(ctx, output_format, _render_pre_merge_report)
