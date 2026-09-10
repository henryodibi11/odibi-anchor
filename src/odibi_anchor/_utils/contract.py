"""Output contract helpers for odibi_anchor tools.

Every tool returns a dict with standard keys. This module provides:
- validate_output_format: Guard against invalid output_format values
- guard_dataframe_type: Guard against non-DataFrame inputs at tool boundaries
- build_base_context: Construct the standard dict scaffold with runtime type checks
- finalize_context: Dispatch to markdown render or return dict
"""

from __future__ import annotations

from typing import Any, Callable

from odibi_anchor._utils.engine_utils import detect_engine


def validate_output_format(output_format: str) -> None:
    """Raise ValueError if output_format is not 'dict', 'markdown', or 'toon'."""
    if output_format not in {"dict", "markdown", "toon"}:
        raise ValueError("output_format must be 'dict', 'markdown', or 'toon'")


def cap_with_hint(
    items: list, limit: int, *, unit: str = "items", param: str = "limit"
) -> tuple[list, str | None]:
    """Cap a list and return (capped_items, size_hint_or_None).

    AXI principle 3 (content truncation): never silently drop data. When a list
    is truncated, the caller emits the returned hint (e.g. into findings) so the
    agent knows data was withheld and how to see the rest.

    Args:
        items: The full list.
        limit: Max items to keep. <= 0 means no cap.
        unit: Human label for the items (for the hint message).
        param: The kwarg an agent would raise to see more.

    Returns:
        (items[:limit], hint) where hint is None if nothing was dropped.
    """
    if limit is None or limit <= 0 or len(items) <= limit:
        return items, None
    dropped = len(items) - limit
    return items[:limit], f"… {dropped} more {unit} omitted (raise {param}= to see all)"


def guard_dataframe_type(df: Any, param_name: str = "df") -> None:
    """Raise TypeError if df is not a Spark or Pandas DataFrame."""
    if detect_engine(df) == "unknown":
        raise TypeError(
            f"{param_name} must be a Spark or Pandas DataFrame, got {type(df).__name__}"
        )


class ContractViolation(TypeError):
    """Raised when a tool output violates the standard contract types."""
    pass


def build_base_context(
    kind: str,
    subject: str,
    summary: str,
    metrics: dict,
    **extra: Any,
) -> dict:
    """Build standard context dict with all contract keys populated.

    Standard keys (always present): kind, subject, summary, metrics,
    findings, risks, samples, suggested_next_actions.

    Any additional keyword arguments are added as extra keys.

    Raises:
        ContractViolation: If any standard key has the wrong type.
    """
    findings = extra.pop("findings", [])
    risks = extra.pop("risks", [])
    samples = extra.pop("samples", {})
    suggested_next_actions = extra.pop("suggested_next_actions", [])

    # ── Runtime contract enforcement ──────────────────────────────────
    if not isinstance(kind, str) or not kind:
        raise ContractViolation(f"kind must be a non-empty str, got {type(kind).__name__}: {kind!r}")
    if not isinstance(subject, str):
        raise ContractViolation(f"subject must be str, got {type(subject).__name__}")
    if not isinstance(summary, str) or not summary:
        raise ContractViolation(f"summary must be a non-empty str, got {type(summary).__name__}: {summary!r}")
    if not isinstance(metrics, dict):
        raise ContractViolation(f"metrics must be dict, got {type(metrics).__name__}")
    if not isinstance(findings, list):
        raise ContractViolation(f"findings must be list, got {type(findings).__name__}")
    if not isinstance(risks, list):
        raise ContractViolation(f"risks must be list, got {type(risks).__name__}")
    if not isinstance(samples, dict):
        raise ContractViolation(f"samples must be dict, got {type(samples).__name__}")
    if not isinstance(suggested_next_actions, list):
        raise ContractViolation(
            f"suggested_next_actions must be list, got {type(suggested_next_actions).__name__}"
        )
    # ──────────────────────────────────────────────────────────────────

    ctx: dict[str, Any] = {
        "kind": kind,
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "findings": findings,
        "risks": risks,
        "samples": samples,
        "suggested_next_actions": suggested_next_actions,
    }
    # Remaining kwargs become additional top-level keys
    ctx.update(extra)
    return ctx


def is_empty_df(df: Any) -> bool:
    """True only for a clearly-empty pandas-like DataFrame (0 rows, has columns).

    Spark DataFrames (no __len__) return False — an emptiness check there requires
    an action and is too expensive for a cheap guard; mirrors the workflow guard.
    Plain lists/dicts return False (they have __len__ but no .columns).
    """
    try:
        return (
            hasattr(df, "__len__")
            and hasattr(df, "columns")
            and len(df) == 0
        )
    except Exception:
        return False


def empty_df_context(kind: str, subject: str | None, tool: str, *, columns=None) -> dict:
    """Definitive empty-state context for a 0-row DataFrame input (AXI principle #5).

    Makes "the input was empty" unmistakable and distinct from "no matches found",
    with concrete recovery steps.
    """
    cols = list(columns) if columns else []
    return build_base_context(
        kind=kind,
        subject=subject or "empty_input",
        summary=f"Input DataFrame has 0 rows — {tool} cannot analyze an empty input.",
        metrics={"row_count": 0, "column_count": len(cols), "columns": cols},
        findings=[
            f"Input to anchor('{tool}', ...) has 0 rows.",
            "This is an explicit EMPTY-INPUT result, not 'no matches found'.",
        ],
        risks=[],
        samples={},
        suggested_next_actions=[
            "Check the upstream filter / join / WHERE clause that produced this DataFrame.",
            "If the empty result is expected, no action needed.",
        ],
    )


def render_empty_df_md(ctx: dict) -> str:
    """Minimal markdown for an empty_df_context (avoids the normal renderers,
    which expect a populated result)."""
    lines = [f"# {ctx.get('kind', 'empty')}: {ctx.get('subject', '')}", "",
             f"**{ctx.get('summary', '')}**", ""]
    for f in ctx.get("findings", []):
        lines.append(f"- {f}")
    if ctx.get("suggested_next_actions"):
        lines.append("")
        lines.append("## Next Actions")
        for a in ctx["suggested_next_actions"]:
            lines.append(f"- {a}")
    return "\n".join(lines)


def finalize_context(
    ctx: dict,
    output_format: str,
    render_fn: Callable[[dict], str],
) -> dict | str:
    """Return dict or rendered markdown based on output_format.

    Call this AFTER building the full context dict. If output_format
    is 'markdown', returns render_fn(ctx); if 'toon', returns selective-TOON
    text; otherwise returns ctx as-is.
    """
    if output_format == "markdown":
        return render_fn(ctx)
    if output_format == "toon":
        from odibi_anchor._utils._toon import render_toon
        return render_toon(ctx)
    return ctx
