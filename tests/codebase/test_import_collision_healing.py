"""Guard for the submodule/function name-collision foot-gun (self-healing __init__).

A full-path import of a submodule that shares a name with a re-exported function
must NOT leave the package attribute shadowed by the module.
"""
import importlib


def test_codebase_map_context_heals_after_full_path_import():
    pkg = importlib.import_module("odibi_anchor.codebase")
    assert callable(pkg.codebase_map_context)
    # Shadow it via a full-path submodule import (the foot-gun)
    importlib.import_module("odibi_anchor.codebase.codebase_map_context")
    # Package attribute access self-heals back to the function
    assert callable(pkg.codebase_map_context)


def test_known_bad_change_context_heals():
    pkg = importlib.import_module("odibi_anchor.codebase")
    importlib.import_module("odibi_anchor.codebase.known_bad_change_context")
    assert callable(pkg.known_bad_change_context)


def test_lazy_loading_still_works():
    pkg = importlib.import_module("odibi_anchor.codebase")
    # Names resolve to callables whether or not previously accessed
    for name in ("import_resolve_context", "memory_context", "learn_context"):
        assert callable(getattr(pkg, name)), name


def test_render_functions_resolve():
    pkg = importlib.import_module("odibi_anchor.codebase")
    assert callable(pkg.render_codebase_map_report)
