"""Planning context generators.

Imports use a hybrid strategy: symbols that share names with submodules
are eagerly imported to avoid module/function ambiguity. Other symbols
use lazy loading via __getattr__ for FUSE performance.
"""

from odibi_anchor.planning.task_execution_context import (
    task_execution_context,
    render_task_execution_report,
    quick_context,
)
from odibi_anchor.planning._task_profile import EvidenceRequest, TaskProfile, normalize_task_profile
from odibi_anchor.planning.context_selection import eye_catalog, select_context

_LAZY_IMPORTS = {
    "handoff_context": ("odibi_anchor.planning.handoff_context", "handoff_context"),
    "render_handoff_report": ("odibi_anchor.planning.handoff_context", "render_handoff_report"),
}


def __getattr__(name):
    if name in _LAZY_IMPORTS:
        import importlib
        module_path, attr_name = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'odibi_anchor.planning' has no attribute {name!r}")


__all__ = [
    "task_execution_context",
    "render_task_execution_report",
    "quick_context",
    "EvidenceRequest",
    "TaskProfile",
    "normalize_task_profile",
    "eye_catalog",
    "select_context",
    "handoff_context",
    "render_handoff_report",
]
