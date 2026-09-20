"""Auto-confirm wrappers — extracted from anchor() closures in agent_init.py.

Each function replaces a closure that captured anchor()'s locals. The captured
state is now passed explicitly as parameters.
"""
from __future__ import annotations


def record_surfaced_result(result, *, db_path=None, primary_keys=("entries",),
                           associated_key="associated_entries") -> int:
    """Record only IDs present in a dispatcher-visible final retrieval result."""
    if not isinstance(result, dict):
        return 0
    ids = []
    for key in (*primary_keys, associated_key):
        for entry in result.get(key, []) or []:
            if isinstance(entry, dict):
                entry_id = entry.get("id") or entry.get("memory_id")
                if entry_id:
                    ids.append(entry_id)
    if not ids:
        return 0
    from odibi_anchor.codebase._memory_db import record_surfaced
    from odibi_anchor.codebase.memory_context import _get_current_session_id
    return record_surfaced(db_path, entry_ids=ids,
                           session_id=_get_current_session_id() or "")


def memory_with_auto_confirm(
    root, args, kwargs, *, memory_context_fn, render_fn,
):
    """Run the read-only anchor("memory") query before optional rendering."""
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    kwargs.setdefault("surfaced", False)
    result = memory_context_fn(root, *args, **kwargs)
    if saved_format == "markdown":
        return render_fn(result)
    return result


def error_with_auto_confirm(
    root, args, kwargs, *, failure_pattern_fn, render_fn,
):
    """Run anchor("known_error") in structured form before optional rendering."""
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    result = failure_pattern_fn(args[0] if args else "", root=root, **kwargs)
    if saved_format == "markdown":
        return render_fn(result)
    return result


def known_bad_with_auto_confirm(
    root, args, kwargs, *, known_bad_fn, add_tag_fn, render_fn,
):
    """Run the read-only anchor("known_bad") query before optional rendering."""
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    result = known_bad_fn(root, *args, **kwargs)
    if saved_format == "markdown":
        return render_fn(result)
    return result


def inject_spec_tag(kwargs: dict, spec_name) -> dict:
    """Append spec:<name> to kwargs['tags'] when a spec is active (S-4).

    Used by anchor("save") so manual memories are linked to the originating spec.
    Mutates and returns kwargs.
    """
    if spec_name:
        tags = list(kwargs.get("tags") or [])
        st = f"spec:{spec_name}"
        if st not in tags:
            tags.append(st)
        kwargs["tags"] = tags
    return kwargs
