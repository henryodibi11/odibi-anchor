"""Behavioral safety tests for the experimental MCP HTTP wrapper."""
from __future__ import annotations

import os
import subprocess
import sys
from importlib import import_module

import pytest

MODULE = "odibi_anchor.mcp_app"


def _import_fresh():
    sys.modules.pop(MODULE, None)
    return import_module(MODULE)


def test_http_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ANCHOR_EXPERIMENTAL_HTTP", raising=False)
    monkeypatch.delenv("ANCHOR_HTTP_ALLOWED_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match=r"disabled by default.*per-client state isolation"):
        _import_fresh()


def test_disabled_clean_import_does_not_load_optional_transport_dependencies():
    script = r'''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "starlette" or name.startswith("starlette.") or name == "fastmcp" or name.startswith("fastmcp.") or name == "odibi_anchor.mcp_server":
        raise AssertionError(f"optional transport imported before gate: {name}")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
try:
    import odibi_anchor.mcp_app
except RuntimeError as exc:
    assert "disabled by default" in str(exc)
else:
    raise AssertionError("disabled import unexpectedly succeeded")
'''
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, ["src", os.environ.get("PYTHONPATH")]))}
    env.pop("ANCHOR_EXPERIMENTAL_HTTP", None)
    env.pop("ANCHOR_HTTP_ALLOWED_ORIGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("origins", [
    "", "*", "file:///tmp", "https://safe.example,*", "https://user@safe.example",
    "https://safe.example/path", "https://safe.example?query=1", "https://safe.example#fragment",
])
def test_experimental_http_requires_safe_explicit_origins(monkeypatch, origins):
    monkeypatch.setenv("ANCHOR_EXPERIMENTAL_HTTP", "1")
    monkeypatch.setenv("ANCHOR_HTTP_ALLOWED_ORIGINS", origins)
    with pytest.raises(RuntimeError, match=r"explicit http\(s\) origins"):
        _import_fresh()


def test_experimental_http_uses_explicit_origins_without_credentials(monkeypatch):
    monkeypatch.setenv("ANCHOR_EXPERIMENTAL_HTTP", "1")
    monkeypatch.setenv("ANCHOR_HTTP_ALLOWED_ORIGINS", "https://one.example,https://two.example:8443")
    module = _import_fresh()
    assert module.ALLOWED_ORIGINS == ("https://one.example", "https://two.example:8443")
    middleware = module.http_app.user_middleware[0]
    assert middleware.kwargs["allow_origins"] == list(module.ALLOWED_ORIGINS)
    assert middleware.kwargs["allow_credentials"] is False
