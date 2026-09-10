"""Shared utility functions for task_execution_context module."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


def _validate_limits(*, max_plan_steps: int, max_items_per_section: int, max_text_length: int) -> None:
    for name, value in {
        "max_plan_steps": max_plan_steps,
        "max_items_per_section": max_items_per_section,
        "max_text_length": max_text_length,
    }.items():
        if not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
    if max_text_length == 0:
        raise ValueError("max_text_length must be > 0")


def _clean_text(value: Any, *, max_text_length: int, metadata: dict[str, Any]) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) > max_text_length:
        metadata["truncated"] = True
        if max_text_length <= 3:
            return text[:max_text_length]
        return text[: max_text_length - 3].rstrip() + "..."
    return text


def _subject_from_task(task: str, *, max_text_length: int, metadata: dict[str, Any]) -> str:
    first_sentence = task.split(".", 1)[0].strip()
    if not first_sentence:
        first_sentence = task
    return _clean_text(first_sentence, max_text_length=min(max_text_length, 90), metadata=metadata) or "unspecified task"


def _normalize_text_list(
    values: Iterable[Any] | str | None,
    *,
    max_items: int,
    max_text_length: int,
    metadata: dict[str, Any],
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, Mapping):
        raise ValueError("Expected a list of text values, not a mapping")
    else:
        raw_values = list(values)

    cleaned = []
    for value in raw_values:
        text = _clean_text(value, max_text_length=max_text_length, metadata=metadata)
        if text is not None:
            cleaned.append(text)
    return _cap_list(_dedupe_preserve_order(cleaned), max_items=max_items, metadata=metadata)


def _normalize_mapping_list(
    values: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    *,
    max_items: int,
    max_text_length: int,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, Mapping):
        raw_values = [values]
    else:
        raw_values = list(values)

    normalized: list[dict[str, Any]] = []
    for item in raw_values:
        if isinstance(item, Mapping):
            normalized_item = {
                str(key): _json_safe(value, max_text_length=max_text_length, metadata=metadata)
                for key, value in item.items()
                if _clean_text(key, max_text_length=max_text_length, metadata=metadata) is not None
            }
        else:
            normalized_item = {"value": _json_safe(item, max_text_length=max_text_length, metadata=metadata)}
        if normalized_item:
            normalized.append(normalized_item)

    return _cap_mapping_list(normalized, max_items=max_items, metadata=metadata)


def _json_safe(value: Any, *, max_text_length: int, metadata: dict[str, Any]) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clean_text(value, max_text_length=max_text_length, metadata=metadata)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, max_text_length=max_text_length, metadata=metadata)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_json_safe(item, max_text_length=max_text_length, metadata=metadata) for item in value]
    if isinstance(value, set):
        return sorted(_json_safe(item, max_text_length=max_text_length, metadata=metadata) for item in value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return _clean_text(value, max_text_length=max_text_length, metadata=metadata)


def _cap_list(values: list[Any], *, max_items: int, metadata: dict[str, Any]) -> list[Any]:
    if max_items == 0:
        if values:
            metadata["truncated"] = True
        return []
    if len(values) > max_items:
        metadata["truncated"] = True
        return values[:max_items]
    return values


def _cap_mapping_list(values: list[dict[str, Any]], *, max_items: int, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    deduped = []
    seen = set()
    for value in values:
        fingerprint = json.dumps(value, sort_keys=True, default=str)
        if fingerprint not in seen:
            seen.add(fingerprint)
            deduped.append(value)
    return _cap_list(deduped, max_items=max_items, metadata=metadata)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_guardrails(
    guardrails: dict[str, Any] | None,
    *,
    out_of_scope: list[str],
    constraints: list[str],
    stop_conditions: list[str],
    max_items: int,
    max_text_length: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build consolidated guardrails block.

    Merges explicit guardrails dict with out_of_scope, constraints, and
    stop_conditions already provided as separate parameters. The guardrails
    dict can contain:
        - do_not_modify: list[str] — files/paths the agent must not change
        - do_not_create: list[str] — files/patterns the agent must not create
        - out_of_scope: list[str] — work explicitly excluded (merged with param)
        - constraints: list[str] — rules the agent must follow (merged with param)
        - stop_conditions: list[str] — when to stop (merged with param)
        - max_files_changed: int — cap on number of files to modify
        - require_verification: list[str] — checks that must pass before declaring done
    """
    base: dict[str, Any] = {
        "do_not_modify": [],
        "do_not_create": [],
        "out_of_scope": list(out_of_scope),
        "constraints": list(constraints),
        "stop_conditions": list(stop_conditions),
        "max_files_changed": None,
        "require_verification": [],
    }

    if guardrails:
        for key in ("do_not_modify", "do_not_create", "out_of_scope",
                     "constraints", "stop_conditions", "require_verification"):
            if key in guardrails and isinstance(guardrails[key], list):
                items = _normalize_text_list(
                    guardrails[key],
                    max_items=max_items,
                    max_text_length=max_text_length,
                    metadata=metadata,
                )
                if key in ("out_of_scope", "constraints", "stop_conditions"):
                    base[key] = _dedupe_preserve_order(base[key] + items)
                else:
                    base[key] = items

        if "max_files_changed" in guardrails:
            val = guardrails["max_files_changed"]
            if isinstance(val, int) and val > 0:
                base["max_files_changed"] = val

    has_any = bool(
        base["do_not_modify"]
        or base["do_not_create"]
        or base["out_of_scope"]
        or base["constraints"]
        or base["stop_conditions"]
        or base["max_files_changed"]
        or base["require_verification"]
    )
    base["has_boundaries"] = has_any

    return base


def _number_plan_steps(steps: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{"step": idx, **step} for idx, step in enumerate(steps, start=1)]
