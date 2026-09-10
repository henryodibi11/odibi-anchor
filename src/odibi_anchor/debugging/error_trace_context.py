"""odibi_anchor.debugging.error_trace_context — Compact context for traceback debugging.

This module turns noisy Python, pandas, Spark, SQL, and Databricks error text
into a small structured packet that is useful for humans and LLMs.

The parser is deterministic and intentionally conservative. It extracts the
most useful evidence, but labels root-cause guidance as likely rather than
certain.

Usage:
    from odibi_anchor.debugging import error_trace_context

    try:
        df["missing_column"]
    except Exception as exc:
        ctx = error_trace_context(exc, df=df, subject="silver.project_fact")
        print(ctx["summary"])
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Mapping
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

_ERROR_TYPE_PATTERN = re.compile(
    r"^\s*(?P<type>(?:[A-Za-z_][\w]*\.)*[A-Za-z_][\w]*(?:Error|Exception|Failure|Warning))\s*:\s*(?P<message>.*)\s*$"
)
_PYTHON_FRAME_PATTERN = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<function>.+?)\s*$'
)
_CELL_FRAME_PATTERN = re.compile(
    r"^\s*Cell In\[(?P<cell>[^\]]+)\], line (?P<line>\d+)\s*$"
)
_SQL_POSITION_PATTERN = re.compile(r"line\s+(?P<line>\d+)\s*,\s*pos\s+(?P<position>\d+)", re.IGNORECASE)
_COLUMN_QUOTE_PATTERN = re.compile(r"['`\"](?P<column>[A-Za-z_][\w .\-/]*)['`\"]")
_SPARK_UNRESOLVED_COLUMN_PATTERN = re.compile(
    r"(?:UNRESOLVED_COLUMN[^\n]*?`(?P<unresolved>[^`]+)`|cannot resolve [`'](?P<cannot_resolve>[^`']+)[`'])",
    re.IGNORECASE,
)
_PANDAS_INDEX_COLUMNS_PATTERN = re.compile(r"\[columns\]", re.IGNORECASE)

_CONTEXT_KEYS_TO_KEEP = {
    "job_id",
    "run_id",
    "task_key",
    "notebook_path",
    "pipeline",
    "step",
    "table",
    "source_table",
    "target_table",
    "user",
    "environment",
}


def error_trace_context(
    error_text: str | BaseException,
    *,
    subject: str | None = None,
    df: Any | None = None,
    engine: str = "auto",
    max_chars: int = 6000,
    sample_limit: int = 5,
    metadata: Mapping[str, Any] | None = None,
    include_dataframe_sample: bool = True,
    output_format: str = "dict",
) -> "dict[str, Any] | str":
    """Generate compact context for a Python, pandas, Spark, SQL, or notebook error.

    Args:
        error_text: Raw traceback/error text or an exception object. Passing an
            exception object is supported; traceback frames are included when the
            exception still carries ``__traceback__``.
        subject: Optional workflow, table, notebook, or step label.
        df: Optional DataFrame related to the failure. Pandas DataFrames are
            summarized now. Spark DataFrame summarization is intentionally left
            as a Databricks follow-up while preserving the public API.
        engine: DataFrame engine for optional ``df`` context. Use ``"auto"``,
            ``"pandas"``, or ``"spark"``. Spark parsing of the error text still
            works without Spark installed.
        max_chars: Maximum characters to keep in ``relevant_trace``.
        sample_limit: Maximum trace frames and DataFrame rows to include.
        metadata: Optional run metadata such as notebook path, job id, table, or
            pipeline step. Only compact scalar values are retained.
        include_dataframe_sample: If True, include up to ``sample_limit`` sample
            rows from a pandas DataFrame.
        output_format: Output format. ``"dict"`` returns the structured context
            dictionary. ``"markdown"`` returns a human-readable markdown report.

    Returns:
        Structured context dictionary containing the error type/message, likely
        error category, failing file/line where available, compact trace excerpt,
        optional DataFrame context, findings, risks, and suggested next actions.

    Example:
        >>> import pandas as pd
        >>> from odibi_anchor.debugging import error_trace_context
        >>> df = pd.DataFrame({"id": [1], "name": ["A"]})
        >>> try:
        ...     df["missing"]
        ... except Exception as exc:
        ...     ctx = error_trace_context(exc, df=df, subject="demo_df")
        >>> ctx["error"]["type"]
        'KeyError'
    """
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200 so the context remains useful.")
    if sample_limit < 0:
        raise ValueError("sample_limit must be greater than or equal to 0.")
    validate_output_format(output_format)

    raw_text = _coerce_error_text(error_text)
    normalized_text = _normalize_text(raw_text)
    lines = normalized_text.splitlines()

    exception_chain = _extract_exception_chain(lines)
    error_type, error_message = _select_primary_error(exception_chain, lines)
    trace_frames = _extract_trace_frames(lines, limit=sample_limit)
    failing_frame = trace_frames[-1] if trace_frames else None
    sql_position = _extract_sql_position(normalized_text)
    category = _classify_error(error_type, error_message, normalized_text)
    column_hints = _extract_column_hints(normalized_text)

    dataframe_context, dataframe_risks = _build_dataframe_context(
        df=df,
        engine=engine,
        sample_limit=sample_limit,
        include_dataframe_sample=include_dataframe_sample,
        column_hints=column_hints,
    )

    relevant_trace, was_truncated = _build_relevant_trace(
        lines=lines,
        trace_frames=trace_frames,
        exception_chain=exception_chain,
        max_chars=max_chars,
    )

    findings = _build_findings(
        error_type=error_type,
        error_message=error_message,
        category=category,
        trace_frames=trace_frames,
        column_hints=column_hints,
        dataframe_context=dataframe_context,
    )
    risks = _build_risks(
        raw_text=normalized_text,
        error_type=error_type,
        was_truncated=was_truncated,
        dataframe_risks=dataframe_risks,
    )
    suggested_next_actions = _suggest_next_actions(

        category=category,
        error_type=error_type,
        column_hints=column_hints,
        dataframe_context=dataframe_context,
    )
    # ── Graph wiring (audit fix) ──
    suggested_next_actions.append('SHOULD: Run anchor("known_error", "error text") to check known failure patterns.')
    suggested_next_actions.append('SHOULD: Run anchor("map") to locate the failing module in codebase.')
    suggested_next_actions.append('After fix: Run anchor("touched") → anchor("preflight") → anchor("test") → anchor("gate").')
    suggested_next_actions.append("SKILL: Load skills/debugging/SKILL.md for evidence-oriented diagnostics.")

    # ── Category-specific skill routing (H-005) ──
    _CATEGORY_SKILL_HINTS = {
        "missing_dependency": [
            "SKILL: Load skills/dependency-management/SKILL.md for import/package resolution.",
        ],
        "schema_or_column_reference": [
            "SKILL: Load skills/schema-design/SKILL.md for column/grain reference issues.",
        ],
        "merge_or_key": [
            "SKILL: Load skills/schema-design/SKILL.md for key/grain integrity before joins.",
        ],
        "type_conversion": [
            "SKILL: Load skills/data-onboarding/SKILL.md for source quality and coercion patterns.",
        ],
        "execution_resource": [
            "SKILL: Load skills/performance-investigation/SKILL.md for OOM/resource failures.",
        ],
        "data_quality": [
            "SKILL: Load skills/data-onboarding/SKILL.md for source data validation failures.",
        ],
    }
    for _hint in _CATEGORY_SKILL_HINTS.get(category, []):
        if _hint not in suggested_next_actions:
            suggested_next_actions.append(_hint)

    compact_metadata = _compact_metadata(metadata)
    location = _build_location(failing_frame, sql_position)
    summary = _build_summary(error_type, error_message, category, location)
    samples = _build_samples(
        trace_frames=trace_frames,
        dataframe_context=dataframe_context,
        sample_limit=sample_limit,
    )

    ctx = {
        "kind": "error_trace_context",
        "subject": subject or compact_metadata.get("step") or compact_metadata.get("table") or "error_trace",
        "summary": summary,
        "metrics": {
            "input_char_count": len(normalized_text),
            "input_line_count": len(lines),
            "relevant_trace_char_count": len(relevant_trace),
            "trace_frame_count": len(trace_frames),
            "exception_chain_count": len(exception_chain),
            "max_chars": max_chars,
            "has_dataframe_context": dataframe_context is not None,
            "dataframe_engine": dataframe_context.get("engine") if dataframe_context else None,
        },
        "error": {
            "type": error_type,
            "message": error_message,
            "category": category,
            "exception_chain": exception_chain,
        },
        "location": location,
        "trace_frames": trace_frames,
        "dataframe_context": dataframe_context,
        "metadata": compact_metadata,
        "findings": findings,
        "risks": risks,
        "samples": {"context_rows": samples} if samples else {},
        "relevant_trace": relevant_trace,
        "suggested_next_actions": suggested_next_actions,
    }

    if output_format == "markdown":
        return render_error_trace_report(ctx)
    return ctx


def _coerce_error_text(error_text: str | BaseException) -> str:
    """Convert raw text or an exception object to traceback text."""
    if isinstance(error_text, BaseException):
        formatted = traceback.format_exception(type(error_text), error_text, error_text.__traceback__)
        return "".join(formatted).strip()
    return str(error_text or "")


def _normalize_text(text: str) -> str:
    """Normalize whitespace without destroying traceback indentation."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    return text.strip()


def _extract_exception_chain(lines: list[str]) -> list[dict[str, str]]:
    """Extract exception-like lines from traceback text in observed order."""
    chain: list[dict[str, str]] = []
    for line in lines:
        match = _ERROR_TYPE_PATTERN.match(line)
        if not match:
            continue
        error_type = match.group("type").split(".")[-1]
        message = match.group("message").strip()
        chain.append({"type": error_type, "message": message})
    return chain


def _select_primary_error(exception_chain: list[dict[str, str]], lines: list[str]) -> tuple[str, str]:
    """Pick the final exception as the primary error."""
    if exception_chain:
        primary = exception_chain[-1]
        return primary["type"] or "UnknownError", primary["message"] or ""

    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return "UnknownError", "No error text provided."

    last_line = non_empty[-1]
    if ":" in last_line:
        maybe_type, maybe_message = last_line.split(":", 1)
        if re.match(r"^(?:[A-Za-z_][\w]*\.)*[A-Za-z_][\w]*(?:Error|Exception|Failure|Warning)$", maybe_type.strip()):
            return maybe_type.strip().split(".")[-1], maybe_message.strip()
    return "UnknownError", last_line[:500]


def _extract_trace_frames(lines: list[str], *, limit: int) -> list[dict[str, Any]]:
    """Extract Python/IPython traceback frames and nearby code lines."""
    frames: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        frame = _parse_python_frame(line) or _parse_cell_frame(line)
        if frame is None:
            continue
        code = _next_code_line(lines, idx)
        if code:
            frame["code"] = code
        frames.append(frame)
    if limit == 0:
        return []
    return frames[-limit:]


def _parse_python_frame(line: str) -> dict[str, Any] | None:
    match = _PYTHON_FRAME_PATTERN.match(line)
    if not match:
        return None
    return {
        "file": match.group("file"),
        "line": int(match.group("line")),
        "function": match.group("function").strip(),
    }


def _parse_cell_frame(line: str) -> dict[str, Any] | None:
    match = _CELL_FRAME_PATTERN.match(line)
    if not match:
        return None
    return {
        "file": f"Cell In[{match.group('cell')}]",
        "line": int(match.group("line")),
        "function": "<cell>",
    }


def _next_code_line(lines: list[str], frame_index: int) -> str | None:
    """Return the next likely source-code line following a traceback frame."""
    for offset in range(1, 4):
        candidate_index = frame_index + offset
        if candidate_index >= len(lines):
            break
        candidate = lines[candidate_index]
        stripped = candidate.strip()
        if not stripped:
            continue
        if stripped.startswith(("File ", "Traceback", "The above exception", "During handling")):
            return None
        if _ERROR_TYPE_PATTERN.match(candidate):
            return None
        if stripped.startswith("^"):
            continue
        return stripped
    return None


def _extract_sql_position(text: str) -> dict[str, int] | None:
    match = _SQL_POSITION_PATTERN.search(text)
    if not match:
        return None
    return {"line": int(match.group("line")), "position": int(match.group("position"))}


def _extract_column_hints(text: str) -> list[str]:
    """Extract likely referenced column names from common pandas/Spark messages."""
    hints: list[str] = []

    for match in _SPARK_UNRESOLVED_COLUMN_PATTERN.finditer(text):
        column = match.group("unresolved") or match.group("cannot_resolve")
        if column:
            hints.append(column)

    for match in _COLUMN_QUOTE_PATTERN.finditer(text):
        value = match.group("column").strip()
        if _looks_like_column_hint(value):
            hints.append(value)

    # Pandas frequently formats list-of-column failures as:
    # "None of [Index(['a', 'b'], dtype='object')] are in the [columns]".
    if _PANDAS_INDEX_COLUMNS_PATTERN.search(text):
        hints.extend(re.findall(r"'([^']+)'", text))

    return _dedupe_preserve_order(hints)[:20]


def _looks_like_column_hint(value: str) -> bool:
    if not value or len(value) > 120:
        return False
    lowered = value.lower()
    reject_tokens = {
        "traceback",
        "columns",
        "index",
        "object",
        "string",
        "integer",
        "double",
        "true",
        "false",
        "none",
    }
    if lowered in reject_tokens:
        return False
    return bool(re.search(r"[A-Za-z_]", value))


def _classify_error(error_type: str, error_message: str, full_text: str) -> str:
    """Classify the error into a broad debugging bucket."""
    text = f"{error_type} {error_message} {full_text}".lower()

    if any(token in text for token in ["syntaxerror", "parseexception", "parse_syntax_error", "syntax error"]):
        return "syntax"
    if any(token in text for token in ["keyerror", "unresolved_column", "cannot resolve", "column", "field not found"]):
        return "schema_or_column_reference"
    if any(token in text for token in ["valueerror", "could not convert", "invalid literal", "astype", "typeerror", "cannot cast"]):
        return "type_conversion"
    if any(token in text for token in ["modulenotfounderror", "importerror", "no module named"]):
        return "missing_dependency"
    if any(token in text for token in ["filenotfounderror", "path does not exist", "no such file", "not found:"]):
        return "file_or_path"
    if any(token in text for token in ["permissionerror", "access denied", "unauthorized", "forbidden", "permission denied"]):
        return "permission"
    if any(token in text for token in ["mergeerror", "one_to_one", "many_to_one", "duplicate", "multiple source rows"]):
        return "merge_or_key"
    if any(token in text for token in ["outofmemory", "executorlostfailure", "timeout", "timed out", "memory"]):
        return "execution_resource"
    if any(token in text for token in ["assertionerror", "validation", "quality", "constraint"]):
        return "data_quality"
    return "unknown"


def _build_dataframe_context(
    *,
    df: Any | None,
    engine: str,
    sample_limit: int,
    include_dataframe_sample: bool,
    column_hints: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build optional DataFrame context, currently implemented for pandas."""
    if df is None:
        return None, []

    resolved_engine = _detect_engine(df) if engine == "auto" else engine
    if resolved_engine == "pandas":
        return _dataframe_context_pandas(
            df=df,
            sample_limit=sample_limit,
            include_dataframe_sample=include_dataframe_sample,
            column_hints=column_hints,
        ), []

    if resolved_engine == "spark":
        return None, ["Spark DataFrame context was not collected in this pandas-first implementation."]

    return None, [f"DataFrame context was not collected for unsupported engine: {resolved_engine}."]


def _detect_engine(df: Any) -> str:
    """Detect pandas or Spark-like DataFrame objects without hard Spark dependency."""
    try:
        import pandas as pd  # type: ignore

        if isinstance(df, pd.DataFrame):
            return "pandas"
    except Exception:
        pass  # SILENT-OK: pandas import probe — expected to fail if pandas not installed

    if hasattr(df, "schema") and hasattr(df, "columns") and hasattr(df, "select"):
        return "spark"
    return type(df).__name__


def _dataframe_context_pandas(
    *,
    df: Any,
    sample_limit: int,
    include_dataframe_sample: bool,
    column_hints: list[str],
) -> dict[str, Any]:
    """Summarize a pandas DataFrame with compact debugging clues."""
    import pandas as pd  # type: ignore

    if not isinstance(df, pd.DataFrame):
        raise TypeError("_dataframe_context_pandas expects a pandas DataFrame.")

    columns = [str(col) for col in df.columns]
    referenced_found = [col for col in column_hints if col in columns]
    referenced_missing = [col for col in column_hints if col not in columns]
    duplicate_columns = [str(col) for col in df.columns[df.columns.duplicated()].tolist()]

    samples: list[dict[str, Any]] = []
    if include_dataframe_sample and sample_limit > 0:
        samples = [_jsonable_record(record) for record in df.head(sample_limit).to_dict(orient="records")]

    return {
        "engine": "pandas",
        "row_count": int(len(df)),
        "column_count": int(len(df.columns)),
        "columns": columns,
        "dtypes": {str(col): str(dtype) for col, dtype in df.dtypes.items()},
        "null_counts": {str(col): int(count) for col, count in df.isna().sum().items()},
        "duplicate_columns": duplicate_columns,
        "referenced_columns_found": referenced_found,
        "referenced_columns_missing": referenced_missing,
        "sample_rows": samples,
    }


def _jsonable_record(record: Mapping[Any, Any]) -> dict[str, Any]:
    """Convert a pandas record to simple JSON-safe values."""
    return {str(key): _jsonable_value(value) for key, value in record.items()}


def _jsonable_value(value: Any) -> Any:
    """Return JSON-friendly scalar values without adding a numpy dependency."""
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass  # SILENT-OK: .item() conversion is best-effort
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass  # SILENT-OK: isoformat conversion is best-effort
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _build_relevant_trace(
    *,
    lines: list[str],
    trace_frames: list[dict[str, Any]],
    exception_chain: list[dict[str, str]],
    max_chars: int,
) -> tuple[str, bool]:
    """Build a compact trace focused on frames and exception lines."""
    if not lines:
        return "", False

    selected: list[str] = []
    if trace_frames:
        selected.append("Trace frames:")
        for frame in trace_frames:
            selected.append(_format_frame(frame))
    else:
        selected.extend(lines[-40:])

    if exception_chain:
        selected.append("Exception chain:")
        for exc in exception_chain:
            line = f"{exc['type']}: {exc['message']}" if exc["message"] else exc["type"]
            selected.append(line)

    relevant_trace = "\n".join(selected).strip()
    if len(relevant_trace) <= max_chars:
        return relevant_trace, False

    suffix = "\n...[truncated to max_chars]"
    keep = max_chars - len(suffix)
    return relevant_trace[:keep].rstrip() + suffix, True


def _format_frame(frame: Mapping[str, Any]) -> str:
    code = frame.get("code")
    base = f"- {frame.get('file')}:{frame.get('line')} in {frame.get('function')}"
    return f"{base} | {code}" if code else base


def _build_findings(
    *,
    error_type: str,
    error_message: str,
    category: str,
    trace_frames: list[dict[str, Any]],
    column_hints: list[str],
    dataframe_context: dict[str, Any] | None,
) -> list[str]:
    findings = [f"Detected {error_type} categorized as {category}."]

    if error_message:
        findings.append(f"Primary error message: {error_message[:300]}")
    if trace_frames:
        findings.append(f"Extracted {len(trace_frames)} traceback frame(s); the last frame is treated as the likely failure location.")
    if column_hints:
        findings.append(f"Detected likely referenced column(s): {', '.join(column_hints[:8])}.")
    if dataframe_context:
        findings.append(
            f"Collected pandas DataFrame context with {dataframe_context['row_count']} row(s) and "
            f"{dataframe_context['column_count']} column(s)."
        )
        missing = dataframe_context.get("referenced_columns_missing") or []
        if missing:
            findings.append(f"Referenced column(s) not present in DataFrame: {', '.join(missing[:8])}.")
    return findings


def _build_risks(
    *,
    raw_text: str,
    error_type: str,
    was_truncated: bool,
    dataframe_risks: list[str],
) -> list[str]:
    risks: list[str] = []
    if not raw_text:
        risks.append("No error text was provided; context is incomplete.")
    if error_type == "UnknownError":
        risks.append("Could not identify an explicit exception type; suggested actions may be broad.")
    if was_truncated:
        risks.append("Relevant trace was truncated to max_chars; some upstream context may be omitted.")
    risks.extend(dataframe_risks)
    risks.append("Root cause is inferred from traceback patterns only; verify against code and data before changing production logic.")
    return risks



# ---------------------------------------------------------------------------
# Spark-Specific Remediation Patterns
# ---------------------------------------------------------------------------

_SPARK_REMEDIATION_PATTERNS: list[dict[str, Any]] = [
    {
        "match_category": "execution_resource",
        "match_text": ["BroadcastTimeout", "broadcast", "timeout"],
        "actions": [
            "Disable auto-broadcast: spark.conf.set('spark.sql.autoBroadcastJoinThreshold', -1)",
            "If broadcast is intentional, increase timeout: spark.conf.set('spark.sql.broadcastTimeout', 600)",
            "Check if the table being broadcast exceeds 10MB — repartition or use sort-merge join instead.",
        ],
    },
    {
        "match_category": "execution_resource",
        "match_text": ["OutOfMemory", "OOM", "SparkOutOfMemoryError", "java.lang.OutOfMemoryError"],
        "actions": [
            "Check for cartesian joins, exploding arrays, or collect() on large datasets.",
            "Reduce partition sizes: df.repartition(n) or spark.conf.set('spark.sql.shuffle.partitions', '400')",
            "Persist intermediate DataFrames with .cache() or .persist() to break long lineage chains.",
            "Increase executor memory or add more executors rather than increasing driver memory.",
        ],
    },
    {
        "match_category": "execution_resource",
        "match_text": ["SpillToDisk", "spill", "skew", "FetchFailed"],
        "actions": [
            "Enable AQE: spark.conf.set('spark.sql.adaptive.enabled', 'true')",
            "For key skew: salt the join key (add random partition suffix, join, remove suffix).",
            "Repartition on the skewed column before the join: df.repartition('skewed_col')",
            "Check Spark UI > Stages for tasks with disproportionate input size.",
        ],
    },
    {
        "match_category": "merge_or_key",
        "match_text": ["MERGE", "DeltaMerge", "null", "NULL"],
        "actions": [
            "Add IS NOT NULL filter to merge condition: AND target.key IS NOT NULL AND source.key IS NOT NULL",
            "Deduplicate the source DataFrame on merge keys BEFORE the merge.",
            "Check for null keys in source: df.filter(F.col('key').isNull()).count()",
            "Validate merge cardinality: source should be 1:1 on merge keys.",
        ],
    },
    {
        "match_category": "schema_or_column_reference",
        "match_text": ["UNRESOLVED_COLUMN", "AnalysisException", "cannot resolve"],
        "actions": [
            "Print available columns: df.columns or df.printSchema()",
            "Check case sensitivity — Spark column references are case-sensitive in some operations.",
            "If column exists in one branch of a union/join but not both, use .select() to align schemas.",
            "Check if the column was dropped or renamed in an earlier transformation step.",
        ],
    },
    {
        "match_category": "type_conversion",
        "match_text": ["Cast", "ClassCastException", "type mismatch", "cannot be cast"],
        "actions": [
            "Use F.col(c).cast('target_type') with try_cast or wrap in F.when/otherwise for safe handling.",
            "Inspect problematic rows: df.filter(F.col(c).cast('int').isNull() & F.col(c).isNotNull())",
            "For Excel/CSV sources: TRY_CAST(NULLIF(TRIM(col), '') AS target_type) — never raw CAST.",
        ],
    },
    {
        "match_category": "execution_resource",
        "match_text": ["timeout", "TimeoutException", "task timed out"],
        "actions": [
            "Reduce data volume for the failing stage — add .where() filters or .limit() for debugging.",
            "Check for expensive UDFs in the plan — replace with native Spark functions where possible.",
            "Increase spark.network.timeout and spark.executor.heartbeatInterval for long-running tasks.",
        ],
    },
    {
        "match_category": "file_or_path",
        "match_text": ["FileNotFoundException", "Path does not exist", "delta", "table not found"],
        "actions": [
            "Verify the table/path exists: spark.catalog.tableExists('catalog.schema.table')",
            "Check Unity Catalog catalog/schema context: spark.sql('USE CATALOG x; USE SCHEMA y')",
            "For Delta: check if the path was vacuumed or if time travel retention expired.",
        ],
    },
]


def _suggest_next_actions(
    *,
    category: str,
    error_type: str,
    column_hints: list[str],
    dataframe_context: dict[str, Any] | None,
) -> list[str]:
    """Return conservative, category-specific debugging actions."""
    actions_by_category = {
        "schema_or_column_reference": [
            "Confirm the failing column or field exists in the input DataFrame/table at this step.",
            "Compare expected columns with the actual schema immediately before the failing line.",
            "Check recent upstream renames, select/drop operations, joins, or schema drift.",
        ],
        "type_conversion": [
            "Inspect the values being converted and isolate rows that cannot be cast safely.",
            "Add explicit coercion or validation before the transformation.",
            "Check whether upstream ingestion changed the source type or introduced sentinel strings.",
        ],
        "syntax": [
            "Review the generated Python/SQL around the reported line and position.",
            "Run the smallest failing statement independently before debugging downstream logic.",
            "Check quote escaping, missing commas, reserved keywords, and incomplete expressions.",
        ],
        "missing_dependency": [
            "Confirm the package is installed on the current cluster/job environment.",
            "Check import name versus package name and cluster library scope.",
            "Add the dependency to the job/cluster configuration only after confirming it is required.",
        ],
        "file_or_path": [
            "Confirm the path/table exists from the same workspace, catalog, and identity running the job.",
            "Check environment-specific path variables and mount/catalog differences.",
            "Validate permissions separately from existence.",
        ],
        "permission": [
            "Confirm the job/service principal/user has access to the object referenced in the failing operation.",
            "Check Unity Catalog grants, storage ACLs, and secret scope permissions.",
            "Avoid retrying until identity and object permissions are verified.",
        ],
        "merge_or_key": [
            "Check duplicate business keys in the source and target before the merge/join.",
            "Validate the intended grain and merge cardinality.",
            "Deduplicate or aggregate explicitly only after deciding the correct business rule.",
        ],
        "execution_resource": [
            "Check data volume, partitioning, skew, and actions that materialize large datasets.",
            "Reduce the repro to the smallest failing slice before scaling cluster resources.",
            "Inspect Spark UI/job metrics for spills, skew, failed executors, and timeout patterns.",
        ],
        "data_quality": [
            "Inspect failed validation rows and confirm whether the rule or data is wrong.",
            "Separate blocking quality failures from warning-level anomalies.",
            "Add a focused validation test once the failure mode is confirmed.",
        ],
        "unknown": [
            "Reproduce the failure with the smallest input and capture the full traceback.",
            "Inspect the last traceback frame first, then move upward only if the local line is a wrapper.",
            "Add dataframe/schema context before sending the issue to Genie Code or another LLM.",
        ],
    }
    actions = list(actions_by_category.get(category, actions_by_category["unknown"]))

    if column_hints and dataframe_context:
        missing = dataframe_context.get("referenced_columns_missing") or []
        found = dataframe_context.get("referenced_columns_found") or []
        if missing:
            actions.insert(0, f"Resolve missing column(s): {', '.join(missing[:5])}.")
        elif found:
            actions.insert(0, f"Column hint(s) exist in the DataFrame; inspect values and transformation logic for: {', '.join(found[:5])}.")

    if error_type == "UnknownError":
        actions.append("Capture the full exception line if available; current error text did not expose a standard exception type.")

    # Append Spark-specific remediation when patterns match
    spark_actions = _match_spark_remediation(category, error_type, column_hints)
    if spark_actions:
        actions.extend(spark_actions)

    # Append anchor() workflow guidance for agent autonomy
    actions.append(
        "MUST: After identifying the fix, run anchor('preflight', changed_files=[...]) "
        "before committing changes."
    )
    if category in ("schema_or_column_reference", "merge_or_key", "data_quality"):
        actions.append(
            "MUST: Run anchor('profile_table', df, subject='...') on the failing DataFrame "
            "to inspect actual schema and values."
        )
    if category == "missing_dependency":
        actions.append(
            "MUST: Run anchor('lookup', 'function_name') to verify correct import path "
            "and package name."
        )

    return _dedupe_preserve_order(actions)



def _match_spark_remediation(
    category: str,
    error_type: str,
    column_hints: list[str],
) -> list[str]:
    """Match Spark-specific remediation patterns against error details."""
    matched_actions: list[str] = []
    # Combine error_type and column_hints into a searchable text
    search_text = f"{error_type} {' '.join(column_hints)}".lower()

    for pattern in _SPARK_REMEDIATION_PATTERNS:
        if pattern["match_category"] != category:
            continue
        # Check if any match_text substring appears in error context
        for text in pattern["match_text"]:
            if text.lower() in search_text or text.lower() in error_type.lower():
                matched_actions.extend(pattern["actions"])
                break

    return matched_actions[:4]  # Cap at 4 Spark-specific actions


def _build_location(failing_frame: dict[str, Any] | None, sql_position: dict[str, int] | None) -> dict[str, Any]:
    location = {
        "failing_file": failing_frame.get("file") if failing_frame else None,
        "failing_line": failing_frame.get("line") if failing_frame else None,
        "failing_function": failing_frame.get("function") if failing_frame else None,
        "code": failing_frame.get("code") if failing_frame else None,
        "sql_line": sql_position.get("line") if sql_position else None,
        "sql_position": sql_position.get("position") if sql_position else None,
    }
    return location


# What each error class usually points at — phrased as a likely direction, not
# a diagnosis, so the agent still verifies against the actual trace.
_CATEGORY_INTERPRETATION: dict[str, str] = {
    "schema_or_column_reference": "usually a column/schema mismatch — check names against the schema right before the failing line",
    "type_conversion": "usually a value that won't cast — look for sentinel strings or mixed types",
    "merge_or_key": "usually about join keys or grain — duplicate or missing keys are common causes",
    "execution_resource": "usually environment/cluster (memory, timeout, shuffle), not the query logic itself",
    "file_or_path": "usually a path or table that is missing or unreadable for this identity",
    "missing_dependency": "usually a package not available in the current cluster/job environment",
    "permission": "usually an access gap, separate from whether the object exists",
    "data_quality": "usually unexpected values in the data itself rather than the code",
    "syntax": "in the generated code around the reported position",
}


def _build_summary(error_type: str, error_message: str, category: str, location: Mapping[str, Any]) -> str:
    message = error_message or "No explicit message."
    message = message.replace("\n", " ")[:180]
    location_part = ""
    if location.get("failing_file") and location.get("failing_line"):
        location_part = f" Likely failing location: {location['failing_file']}:{location['failing_line']}."
    elif location.get("sql_line") is not None:
        location_part = f" SQL position: line {location['sql_line']}, pos {location['sql_position']}."
    interpretation = _CATEGORY_INTERPRETATION.get(category)
    interp_part = f" This class of error is {interpretation}." if interpretation else ""
    return f"{error_type} ({category}): {message}.{location_part}{interp_part}"


def _build_samples(
    *,
    trace_frames: list[dict[str, Any]],
    dataframe_context: dict[str, Any] | None,
    sample_limit: int,
) -> list[dict[str, Any]]:
    if sample_limit == 0:
        return []

    samples: list[dict[str, Any]] = []
    for frame in trace_frames[-sample_limit:]:
        sample = dict(frame)
        sample["sample_type"] = "trace_frame"
        samples.append(sample)

    if dataframe_context:
        for row in dataframe_context.get("sample_rows", [])[:sample_limit]:
            samples.append({"sample_type": "dataframe_row", "row": row})
    return samples[: sample_limit * 2]


def _compact_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}

    compact: dict[str, Any] = {}
    for key, value in metadata.items():
        key_str = str(key)
        if key_str not in _CONTEXT_KEYS_TO_KEEP:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            value_str = value if not isinstance(value, str) else value[:300]
            compact[key_str] = value_str
        else:
            compact[key_str] = str(value)[:300]
    return compact


def _dedupe_preserve_order(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    deduped: list[Any] = []
    for value in values:
        marker = value if isinstance(value, (str, int, float, bool, tuple)) else repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(value)
    return deduped



def render_error_trace_report(
    ctx: dict,
    *,
    show_trace: bool = True,
    show_dataframe_context: bool = True,
) -> str:
    """Render an error trace context dict as structured markdown.

    Produces a compact debugging report for LLM prompts or human review.

    Args:
        ctx: Dictionary from ``error_trace_context()``.
        show_trace: If True, include the relevant traceback excerpt.
        show_dataframe_context: If True, include DataFrame context details.

    Returns:
        Markdown-formatted string.

    Raises:
        ValueError: If ctx is missing required keys.
    """
    required = {"kind", "subject", "summary", "error"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    error = ctx["error"]
    location = ctx.get("location") or {}
    metrics = ctx.get("metrics") or {}

    # Header
    lines.append(f"# Error Trace: {ctx['subject']}")
    lines.append("")
    lines.append(f"\u274c **{error['type']}** ({error.get('category', 'unknown')})")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Error details
    lines.append("## Error")
    lines.append("")
    lines.append(f"- **Type:** `{error['type']}`")
    lines.append(f"- **Message:** {error.get('message', '')}")
    lines.append(f"- **Category:** {error.get('category', 'unknown')}")
    chain = error.get("exception_chain") or []
    if len(chain) > 1:
        lines.append(f"- **Chain depth:** {len(chain)} (chained exceptions)")
    lines.append("")

    # Location
    if any(location.get(k) for k in ("failing_file", "sql_line")):
        lines.append("## Location")
        lines.append("")
        if location.get("failing_file"):
            lines.append(f"- **File:** `{location['failing_file']}`")
        if location.get("failing_line"):
            lines.append(f"- **Line:** {location['failing_line']}")
        if location.get("failing_function"):
            lines.append(f"- **Function:** `{location['failing_function']}`")
        if location.get("code"):
            lines.append(f"- **Code:** `{location['code']}`")
        if location.get("sql_line"):
            lines.append(f"- **SQL line:** {location['sql_line']}, pos {location.get('sql_position', '?')}")
        lines.append("")

    # Metrics
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Input chars | {metrics.get('input_char_count', 0):,} |")
    lines.append(f"| Input lines | {metrics.get('input_line_count', 0):,} |")
    lines.append(f"| Trace frames | {metrics.get('trace_frame_count', 0)} |")
    lines.append(f"| Exception chain | {metrics.get('exception_chain_count', 0)} |")
    lines.append(f"| Has DataFrame | {metrics.get('has_dataframe_context', False)} |")
    lines.append("")

    # DataFrame context
    df_ctx = ctx.get("dataframe_context")
    if show_dataframe_context and df_ctx:
        lines.append("## DataFrame Context")
        lines.append("")
        lines.append(f"- **Engine:** {df_ctx.get('engine', '?')}")
        lines.append(f"- **Rows:** {df_ctx.get('row_count', '?')}")
        lines.append(f"- **Columns:** {df_ctx.get('column_count', '?')}")
        cols = df_ctx.get("columns") or []
        if cols:
            lines.append(f"- **Available:** {', '.join(f'`{c}`' for c in cols[:15])}")
        missing = df_ctx.get("referenced_columns_missing") or []
        if missing:
            lines.append(f"- **Missing (referenced):** {', '.join(f'`{c}`' for c in missing)}")
        lines.append("")

    # Findings
    findings = ctx.get("findings") or []
    if findings:
        lines.append("## Findings")
        lines.append("")
        for f in findings:
            if isinstance(f, str):
                lines.append(f"- {f}")
            elif isinstance(f, dict):
                lines.append(f"- [{f.get('severity', 'info')}] {f.get('detail', f)}")
        lines.append("")

    # Risks
    risks = ctx.get("risks") or []
    if risks:
        lines.append("## Risks")
        lines.append("")
        for r in risks:
            if isinstance(r, str):
                lines.append(f"- \u26a0\ufe0f {r}")
            elif isinstance(r, dict):
                lines.append(f"- \u26a0\ufe0f {r.get('message', r)}")
        lines.append("")

    # Relevant trace
    if show_trace and ctx.get("relevant_trace"):
        lines.append("## Relevant Trace")
        lines.append("")
        lines.append("```python")
        lines.append(ctx["relevant_trace"])
        lines.append("```")
        lines.append("")

    # Suggested next actions
    actions = ctx.get("suggested_next_actions") or []
    if actions:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for i, action in enumerate(actions, 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    # Metadata
    metadata = ctx.get("metadata") or {}
    if metadata:
        lines.append("## Metadata")
        lines.append("")
        for k, v in metadata.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    return "\n".join(lines)
