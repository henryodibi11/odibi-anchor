"""Exact compatibility surface for peer-neutral readiness document validators."""

from __future__ import annotations

from odibi_anchor._governance_protocol import _documents as _peer

COMMIT = _peer.COMMIT
EFFECTS = _peer.EFFECTS
ID_PATTERN = _peer.ID_PATTERN
PCHAR = _peer.PCHAR
PYTHON_MINOR = _peer.PYTHON_MINOR
SCOPES = _peer.SCOPES
SHA = _peer.SHA
SOURCES = _peer.SOURCES
UNRESERVED = _peer.UNRESERVED
Any = _peer.Any
Callable = _peer.Callable
CanonicalError = _peer.CanonicalError
PurePath = _peer.PurePath
ReadinessProtocolError = _peer.ReadinessProtocolError
_absolute = _peer._absolute
_adapter = _peer._adapter
_array = _peer._array
_attestor = _peer._attestor
_b64 = _peer._b64
_bounded_text = _peer._bounded_text
_confirmation = _peer._confirmation
_digest = _peer._digest
_fail = _peer._fail
_fence = _peer._fence
_id = _peer._id
_manifest = _peer._manifest
_modifiers = _peer._modifiers
_obj = _peer._obj
_provenance = _peer._provenance
_receipt = _peer._receipt
_release = _peer._release
_sorted_unique = _peer._sorted_unique
_text = _peer._text
_tool = _peer._tool
_validate_bootstrap_payload = _peer._validate_bootstrap_payload
base64 = _peer.base64
normalize_repository_id = _peer.normalize_repository_id
normalize = _peer.normalize
re = _peer.re
safe_integer = _peer.safe_integer
string = _peer.string
urlsplit = _peer.urlsplit
urlunsplit = _peer.urlunsplit
validate_bootstrap_payload = _peer.validate_bootstrap_payload

__all__ = (
    "COMMIT",
    "EFFECTS",
    "ID_PATTERN",
    "PCHAR",
    "PYTHON_MINOR",
    "SCOPES",
    "SHA",
    "SOURCES",
    "UNRESERVED",
    "Any",
    "Callable",
    "CanonicalError",
    "PurePath",
    "ReadinessProtocolError",
    "annotations",
    "base64",
    "normalize",
    "normalize_repository_id",
    "re",
    "safe_integer",
    "string",
    "urlsplit",
    "urlunsplit",
    "validate_bootstrap_payload",
)
