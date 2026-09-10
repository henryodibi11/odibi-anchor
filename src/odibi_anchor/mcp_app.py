"""Experimental HTTP app wrapper, disabled by default.

HTTP does not provide per-client odibi-anchor state isolation. It is deprecated
for production use pending a separately designed authenticated, isolated transport.

Usage:
    uvicorn odibi_anchor.mcp_app:http_app --host 0.0.0.0 --port 8000

Environment variables:
    ANCHOR_PROJECT_ID — explicit managed project ID (preferred)
    ANCHOR_PROJECT_ROOT — exact registered target-root hint
    ANCHOR_ALLOW_LEGACY_SELECTOR — set to 1 only for legacy selector fallback
    ANCHOR_EXPERIMENTAL_HTTP — must be 1 to enable this deprecated transport
    ANCHOR_HTTP_ALLOWED_ORIGINS — comma-separated explicit HTTP(S) origins
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

HTTP_GATE_ENV = "ANCHOR_EXPERIMENTAL_HTTP"
ORIGINS_ENV = "ANCHOR_HTTP_ALLOWED_ORIGINS"

if os.environ.get(HTTP_GATE_ENV) != "1":
    raise RuntimeError(
        f"HTTP transport is disabled by default; set {HTTP_GATE_ENV}=1 only for "
        "experimental/deprecated use without per-client state isolation"
    )

ALLOWED_ORIGINS = tuple(
    origin.strip() for origin in os.environ.get(ORIGINS_ENV, "").split(",") if origin.strip()
)


def _is_explicit_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and "*" not in origin
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


if not ALLOWED_ORIGINS or any(not _is_explicit_origin(origin) for origin in ALLOWED_ORIGINS):
    raise RuntimeError(f"{ORIGINS_ENV} must contain explicit http(s) origins; wildcards are forbidden")

# Optional imports intentionally occur only after the standard-library gates. A
# base installation must fail with the documented disabled error, not an incidental
# missing-dependency exception or FastMCP stream side effect.
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

from odibi_anchor.mcp_server import mcp  # noqa: E402

# FastMCP's stateless HTTP option does not isolate odibi-anchor's process state.
http_app = mcp.http_app(stateless_http=True)

http_app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
