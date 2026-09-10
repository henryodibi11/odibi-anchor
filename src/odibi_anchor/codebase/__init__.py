"""odibi_anchor.codebase — Code understanding and analysis tools.

All imports are lazy-loaded via __getattr__ to avoid ~530ms FUSE latency at boot.
"""

# Module → exported names mapping
_LAZY_MODULES = {
    "codebase_map_context": ("odibi_anchor.codebase.codebase_map_context",
        ["codebase_map_context", "render_codebase_map_report"]),
    "session_snapshot_context": ("odibi_anchor.codebase.session_snapshot_context",
        ["session_snapshot_context", "render_session_snapshot_report", "save_snapshot", "load_snapshot"]),
    "consistency_check_context": ("odibi_anchor.codebase.consistency_check_context",
        ["consistency_check_context", "render_consistency_check_report"]),
    "change_impact_context": ("odibi_anchor.codebase.change_impact_context",
        ["change_impact_context", "render_change_impact_report"]),
    "convention_preflight_context": ("odibi_anchor.codebase.convention_preflight_context",
        ["convention_preflight_context", "render_convention_preflight_report"]),
    "focus_context": ("odibi_anchor.codebase.focus_context",
        ["test_focus_context", "render_test_focus_report"]),
    "framework_lookup_context": ("odibi_anchor.codebase.framework_lookup_context",
        ["framework_lookup_context", "render_framework_lookup_report"]),
    "workflow_gate_context": ("odibi_anchor.codebase.workflow_gate_context",
        ["workflow_gate_context", "render_workflow_gate_report"]),
    "memory_context": ("odibi_anchor.codebase.memory_context",
        ["memory_context", "render_memory_report", "append_memory", "confirm_memory",
         "reject_memory", "archive_stale_entries", "export_markdown", "import_from_markdown"]),
    "semantic_edit_context": ("odibi_anchor.codebase.semantic_edit_context",
        ["semantic_edit_context", "render_semantic_edit_report"]),
    "preflight_context": ("odibi_anchor.codebase.preflight_context",
        ["preflight_context", "render_preflight_report"]),
    "import_resolve_context": ("odibi_anchor.codebase.import_resolve_context",
        ["import_resolve_context", "render_import_resolve_report"]),
    "safe_change_context": ("odibi_anchor.codebase.safe_change_context",
        ["safe_change_context", "render_safe_change_report"]),
    "learn_context": ("odibi_anchor.codebase.learn_context",
        ["learn_context", "render_learn_report"]),
    "known_bad_change_context": ("odibi_anchor.codebase.known_bad_change_context",
        ["known_bad_change_context", "render_known_bad_change_report"]),
}

# Build reverse lookup: attribute name → module path
_ATTR_TO_MODULE = {}
for _mod_key, (_mod_path, _names) in _LAZY_MODULES.items():
    for _name in _names:
        _ATTR_TO_MODULE[_name] = _mod_path


def __getattr__(name: str):
    """Lazy import: only load modules when their attributes are first accessed."""
    if name in _ATTR_TO_MODULE:
        import importlib
        mod = importlib.import_module(_ATTR_TO_MODULE[name])
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'odibi_anchor.codebase' has no attribute {name!r}")


__all__ = [
    "codebase_map_context", "render_codebase_map_report",
    "change_impact_context", "render_change_impact_report",
    "session_snapshot_context", "render_session_snapshot_report",
    "save_snapshot", "load_snapshot",
    "consistency_check_context", "render_consistency_check_report",
    "convention_preflight_context", "render_convention_preflight_report",
    "test_focus_context", "render_test_focus_report",
    "framework_lookup_context", "render_framework_lookup_report",
    "workflow_gate_context", "render_workflow_gate_report",
    "memory_context", "render_memory_report",
    "append_memory", "confirm_memory", "reject_memory",
    "archive_stale_entries", "export_markdown", "import_from_markdown",
    "semantic_edit_context", "render_semantic_edit_report",
    "preflight_context", "render_preflight_report",
    "import_resolve_context", "render_import_resolve_report",
    "safe_change_context", "render_safe_change_report",
    "learn_context", "render_learn_report",
    "known_bad_change_context", "render_known_bad_change_report",
]


# ── Self-healing against submodule/function name collisions ──────────────────
# Several submodules share a name with the function they export (e.g.
# `codebase_map_context.py` defines `codebase_map_context`). A full-path import —
# `from odibi_anchor.codebase.codebase_map_context import codebase_map_context`
# — binds the SUBMODULE as a package attribute, shadowing the re-exported
# function, so a later `from odibi_anchor.codebase import codebase_map_context`
# yields the MODULE ("'module' object is not callable"). This was a latent,
# import-order-dependent foot-gun (it surfaced across test files). The override
# below heals it: if a re-exported name resolves to its own submodule, the
# function is re-resolved and rebound. Lazy loading (PEP 562 __getattr__) still
# works because we delegate to the base ModuleType.__getattribute__.
import sys as _sys
import types as _types


class _SelfHealingModule(_types.ModuleType):
    def __getattribute__(self, name):
        val = super().__getattribute__(name)
        if isinstance(val, _types.ModuleType):
            mapping = super().__getattribute__("_ATTR_TO_MODULE")
            if name in mapping:
                import importlib
                func = getattr(importlib.import_module(mapping[name]), name)
                object.__setattr__(self, name, func)
                return func
        return val


_sys.modules[__name__].__class__ = _SelfHealingModule
