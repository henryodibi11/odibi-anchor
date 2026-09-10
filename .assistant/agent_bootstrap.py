"""Resolve one Odibi Anchor checkout and run its authoritative bootstrap."""

from __future__ import annotations

if "__file__" not in globals():
    raise RuntimeError(
        "Odibi Anchor bootstrap requires __file__; use "
        "runpy.run_path(<exact .assistant/agent_bootstrap.py>) in the persistent Python process"
    )

import os
import runpy
from collections.abc import Mapping
from pathlib import Path


def _validated_checkout(raw: object, *, source: str) -> Path:
    if not isinstance(raw, (str, os.PathLike)) or isinstance(raw, bytes):
        raise RuntimeError(f"{source} must be an absolute, non-tilde path")
    value = os.fspath(raw)
    if not isinstance(value, str) or not value or value.startswith("~") or not Path(value).is_absolute():
        raise RuntimeError(f"{source} must be an absolute, non-tilde path")
    checkout = Path(value).resolve(strict=False)
    markers = (
        checkout / "agent_bootstrap.py",
        checkout / "src" / "odibi_anchor" / "bootstrap.py",
    )
    if not checkout.is_dir() or not all(marker.is_file() for marker in markers):
        raise RuntimeError(
            f"{source} is not a Odibi Anchor source checkout: {checkout}; "
            "expected agent_bootstrap.py and src/odibi_anchor/bootstrap.py"
        )
    return checkout.resolve(strict=True)


try:
    _launcher = Path(__file__).resolve(strict=True)
except (OSError, TypeError) as exc:
    raise RuntimeError("Odibi Anchor launcher __file__ is invalid") from exc

if "ANCHOR_SOURCE_CHECKOUT" in globals():
    _checkout = _validated_checkout(
        globals()["ANCHOR_SOURCE_CHECKOUT"], source="ANCHOR_SOURCE_CHECKOUT init global"
    )
elif "ANCHOR_SOURCE_CHECKOUT" in os.environ:
    _checkout = _validated_checkout(
        os.environ["ANCHOR_SOURCE_CHECKOUT"], source="ANCHOR_SOURCE_CHECKOUT environment variable"
    )
else:
    _assistant_parent = _launcher.parent.parent
    _source_candidate = _assistant_parent
    _copied_candidate = _assistant_parent / "odibi_anchor"
    try:
        _checkout = _validated_checkout(_source_candidate, source="source-tree topology")
    except RuntimeError:
        try:
            _checkout = _validated_checkout(_copied_candidate, source="copied-tree topology")
        except RuntimeError as exc:
            raise RuntimeError(
                "No Odibi Anchor checkout exists at the source-tree location or fixed "
                "sibling 'odibi_anchor'. Set one exact absolute, non-tilde "
                "ANCHOR_SOURCE_CHECKOUT path; no workspace scan was attempted."
            ) from exc

_delegate_globals = {}
if "ANCHOR_REPOSITORY_PROVIDER" in globals():
    _delegate_globals["ANCHOR_REPOSITORY_PROVIDER"] = globals()["ANCHOR_REPOSITORY_PROVIDER"]
_namespace = runpy.run_path(
    str(_checkout / "agent_bootstrap.py"),
    init_globals=_delegate_globals,
    run_name="__odibi_anchor_checkout_bootstrap__",
)

_required = ("anchor", "ROOT", "MANIFEST", "ORIENTATION", "BOOTSTRAP")
_missing = tuple(name for name in _required if name not in _namespace)
if _missing:
    raise RuntimeError(f"Odibi Anchor bootstrap omitted required values: {', '.join(_missing)}")
_bootstrap = _namespace["BOOTSTRAP"]
if not isinstance(_bootstrap, Mapping) or _bootstrap.get("success") is not True:
    raise RuntimeError("Odibi Anchor bootstrap did not report success")
try:
    _reported_repository = Path(_bootstrap["repository"]).resolve(strict=True)
except (KeyError, OSError, TypeError, ValueError) as exc:
    raise RuntimeError("Odibi Anchor bootstrap reported an invalid repository") from exc
if _reported_repository != _checkout:
    raise RuntimeError(
        "Odibi Anchor bootstrap repository does not match the selected checkout: "
        f"{_reported_repository} != {_checkout}"
    )

anchor = _namespace["anchor"]
ROOT = _namespace["ROOT"]
MANIFEST = _namespace["MANIFEST"]
ORIENTATION = _namespace["ORIENTATION"]
BOOTSTRAP = _bootstrap
