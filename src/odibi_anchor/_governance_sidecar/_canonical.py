"""Exact compatibility surface for peer-neutral canonicalization."""

from __future__ import annotations

from odibi_anchor._governance_protocol import _canonical as _peer

JS_SAFE_INTEGER = _peer.JS_SAFE_INTEGER
MAX_DEPTH = _peer.MAX_DEPTH
MAX_ITEMS = _peer.MAX_ITEMS
MAX_STRING_BYTES = _peer.MAX_STRING_BYTES
Any = _peer.Any
CanonicalError = _peer.CanonicalError
Mapping = _peer.Mapping
Sequence = _peer.Sequence
_string = _peer._string
domain_digest = _peer.domain_digest
domain_preimage = _peer.domain_preimage
dumps = _peer.dumps
hashlib = _peer.hashlib
normalize = _peer.normalize
safe_integer = _peer.safe_integer
unicodedata = _peer.unicodedata

__all__ = (
    "JS_SAFE_INTEGER",
    "MAX_DEPTH",
    "MAX_ITEMS",
    "MAX_STRING_BYTES",
    "Any",
    "CanonicalError",
    "Mapping",
    "Sequence",
    "annotations",
    "domain_digest",
    "domain_preimage",
    "dumps",
    "hashlib",
    "normalize",
    "safe_integer",
    "unicodedata",
)
