"""Shared internal utilities for odibi_anchor.

Imports are lazy-loaded via __getattr__ to avoid ~500ms FUSE latency at boot.
Only _session_state is eager (shared singleton needed immediately).
"""

# Session state singleton — MUST stay eager (shared between exec'd and imported namespaces)
from odibi_anchor._utils import _session_state  # noqa: F401
from odibi_anchor._utils._session_state import log_note, get_log

# Lazy-loaded module mapping: attribute name → (module_path, name_in_module)
_LAZY_IMPORTS = {
    # engine_utils
    "detect_engine": ("odibi_anchor._utils.engine_utils", "detect_engine"),
    "is_spark_df": ("odibi_anchor._utils.engine_utils", "is_spark_df"),
    "is_pandas_df": ("odibi_anchor._utils.engine_utils", "is_pandas_df"),
    # contract
    "validate_output_format": ("odibi_anchor._utils.contract", "validate_output_format"),
    "build_base_context": ("odibi_anchor._utils.contract", "build_base_context"),
    "finalize_context": ("odibi_anchor._utils.contract", "finalize_context"),
    # render_utils
    "render_header_lines": ("odibi_anchor._utils.render_utils", "render_header_lines"),
    "render_metrics_lines": ("odibi_anchor._utils.render_utils", "render_metrics_lines"),
    "render_bullet_section": ("odibi_anchor._utils.render_utils", "render_bullet_section"),
    "render_numbered_section": ("odibi_anchor._utils.render_utils", "render_numbered_section"),
    "render_table": ("odibi_anchor._utils.render_utils", "render_table"),
    "format_metric": ("odibi_anchor._utils.render_utils", "format_metric"),
    "format_status_badge": ("odibi_anchor._utils.render_utils", "format_status_badge"),
    # ast_utils
    "walk_py_files": ("odibi_anchor._utils.ast_utils", "walk_py_files"),
    "parse_file_safe": ("odibi_anchor._utils.ast_utils", "parse_file_safe"),
    "parse_source_safe": ("odibi_anchor._utils.ast_utils", "parse_source_safe"),
    "extract_imports": ("odibi_anchor._utils.ast_utils", "extract_imports"),
    "extract_functions": ("odibi_anchor._utils.ast_utils", "extract_functions"),
    "extract_classes": ("odibi_anchor._utils.ast_utils", "extract_classes"),
    "get_file_info": ("odibi_anchor._utils.ast_utils", "get_file_info"),
    "read_source_cached": ("odibi_anchor._utils.ast_utils", "read_source_cached"),
    "ast_cache_clear": ("odibi_anchor._utils.ast_utils", "ast_cache_clear"),
    "ast_cache_invalidate": ("odibi_anchor._utils.ast_utils", "ast_cache_invalidate"),
    "ast_cache_stats": ("odibi_anchor._utils.ast_utils", "ast_cache_stats"),
}


def __getattr__(name: str):
    """Lazy import: only load modules when their attributes are first accessed."""
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr_name)
        # Cache in module globals to avoid repeated __getattr__ calls
        globals()[name] = val
        return val
    raise AttributeError(f"module 'odibi_anchor._utils' has no attribute {name!r}")


__all__ = [
    "detect_engine", "is_spark_df", "is_pandas_df",
    "validate_output_format", "build_base_context", "finalize_context",
    "render_header_lines", "render_metrics_lines", "render_bullet_section",
    "render_numbered_section", "render_table", "format_metric", "format_status_badge",
    "walk_py_files", "parse_file_safe", "parse_source_safe",
    "extract_imports", "extract_functions", "extract_classes",
    "get_file_info", "read_source_cached", "ast_cache_clear", "ast_cache_invalidate", "ast_cache_stats",
    "log_note", "get_log",
]
