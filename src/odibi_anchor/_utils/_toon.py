"""TOON (Token-Oriented Object Notation) encoder for anchor() output.

TOON is a compact, lossless-for-LLM encoding of the JSON data model. Its win is
on *uniform arrays of flat objects*, which it renders as a single header row plus
CSV-style data rows (eliminating repeated keys/braces/quotes — ~30-60% fewer
tokens). Its weak spot is deeply nested / non-uniform data, where compact JSON is
smaller; this encoder therefore falls back to compact JSON for those values.

Design: SELECTIVE. The anchor() contract envelope (kind/subject/summary/...) is
non-uniform, so it stays as `key: value` lines; only values that are uniform
arrays of flat dicts (or dicts-of-flat-dicts) become tabular blocks. This keeps
the wins where they exist and avoids TOON's documented failure mode.

Reference: https://github.com/toon-format/toon
"""

from __future__ import annotations

import json
from typing import Any

_SCALAR = (str, int, float, bool, type(None))


def _is_scalar(x: Any) -> bool:
    return isinstance(x, _SCALAR)


def _uniform_table_fields(value: Any) -> list[str] | None:
    """Return ordered field names if value is a non-empty list of flat dicts that
    share the same key set with scalar-only values; else None (not tabular)."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(row, dict) for row in value):
        return None
    keys = list(value[0].keys())
    kset = set(keys)
    for row in value:
        if set(row.keys()) != kset:
            return None
        if not all(_is_scalar(row[k]) for k in keys):
            return None
    return keys


def _dict_of_flat_dicts_fields(value: Any) -> list[str] | None:
    """Return ordered field names if value is a non-empty dict whose values are
    flat dicts sharing the same scalar key set; else None.

    Encoded as a table with a leading `_key` column holding the outer key.
    """
    if not isinstance(value, dict) or not value:
        return None
    rows = list(value.values())
    if not all(isinstance(r, dict) for r in rows):
        return None
    keys = list(rows[0].keys())
    kset = set(keys)
    for r in rows:
        if set(r.keys()) != kset:
            return None
        if not all(_is_scalar(r[k]) for k in keys):
            return None
    return keys


def _scalar_cell(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    s = str(x)
    # Quote only when the comma delimiter or a newline would make the cell
    # ambiguous, or leading/trailing whitespace would be lost.
    if "," in s or "\n" in s or s != s.strip():
        return json.dumps(s)
    return s


def _emit(key: str, value: Any, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    table_fields = _uniform_table_fields(value)
    if table_fields is not None:
        out.append(f"{pad}{key}[{len(value)}]{{{','.join(table_fields)}}}:")
        cpad = "  " * (indent + 1)
        for row in value:
            out.append(cpad + ",".join(_scalar_cell(row[f]) for f in table_fields))
        return

    dod_fields = _dict_of_flat_dicts_fields(value)
    if dod_fields is not None:
        out.append(f"{pad}{key}[{len(value)}]{{_key,{','.join(dod_fields)}}}:")
        cpad = "  " * (indent + 1)
        for outer_key, row in value.items():
            cells = [_scalar_cell(outer_key)] + [_scalar_cell(row[f]) for f in dod_fields]
            out.append(cpad + ",".join(cells))
        return

    if isinstance(value, dict):
        out.append(f"{pad}{key}:")
        for k, v in value.items():
            _emit(str(k), v, indent + 1, out)
        return

    if isinstance(value, list) and all(_is_scalar(x) for x in value):
        if not value:
            out.append(f"{pad}{key}[0]:")
        else:
            out.append(f"{pad}{key}[{len(value)}]: " + ", ".join(_scalar_cell(x) for x in value))
        return

    if _is_scalar(value):
        out.append(f"{pad}{key}: {_scalar_cell(value)}")
        return

    # Nested / non-uniform (list of dicts with differing shapes, lists of lists,
    # dicts with list values, etc.) — compact JSON wins here; TOON would not.
    out.append(f"{pad}{key}: " + json.dumps(value, separators=(",", ":"), default=str))


def render_toon(ctx: dict) -> str:
    """Encode a anchor() context dict as selective TOON.

    Uniform tabular sub-structures become header+rows blocks; everything else
    stays as `key: value` lines or compact JSON. Lossless for LLM consumption.
    """
    if not isinstance(ctx, dict):
        return json.dumps(ctx, separators=(",", ":"), default=str)
    out: list[str] = []
    for key, value in ctx.items():
        _emit(str(key), value, 0, out)
    return "\n".join(out)
