"""Recursive credential and signed-URL redaction."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._contract import normalize_json

REDACTED = "<redacted>"
_SECRET_KEY = re.compile(r"(?i)(authorization|cookie|password|passwd|secret|token|api[-_]?key|private[-_]?key|credential)")
_SIGNED_QUERY = re.compile(r"(?i)^(x-amz-|x-goog-|sig(nature)?$|token$|se$|sp$|sv$|key$|credential$)")
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:authorization|password|passwd|secret|token|api[-_]?key|private[-_]?key)\b\s*[=:]\s*)"
    r"((?:bearer|basic)\s+[^\s,;&]+|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")


def _sanitize_url(url: str, categories: set[str]) -> tuple[str, int]:
    """Remove userinfo and signed query values from one absolute URI."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, 0
    count = 0
    if parts.username is not None or parts.password is not None:
        host = parts.hostname or ""
        if parts.port is not None:
            host += f":{parts.port}"
        parts = parts._replace(netloc=f"{REDACTED}@{host}")
        categories.add("connection_credentials")
        count += 1
    if parts.query:
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if _SIGNED_QUERY.search(key):
                value = REDACTED
                categories.add("signed_url")
                count += 1
            query.append((key, value))
        parts = parts._replace(query=urlencode(query))
    return urlunsplit(parts), count


def _redact_text(text: str, categories: set[str]) -> tuple[str, int]:
    count = 0

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        categories.add("credentials")
        return match.group(1) + REDACTED

    text = _ASSIGNMENT.sub(assignment, text)
    replaced, found = _BEARER.subn(lambda match: f"{match.group(1)} {REDACTED}", text)
    if found:
        categories.add("authorization")
        count += found
    text = replaced
    def url_replacement(match: re.Match[str]) -> str:
        nonlocal count
        cleaned, changes = _sanitize_url(match.group(0), categories)
        count += changes
        return cleaned

    text = _URL.sub(url_replacement, text)
    return text, count


def redact(value: Any) -> tuple[Any, dict[str, Any]]:
    """Return a JSON value with nested credentials removed and an audit summary."""
    value = normalize_json(value)
    categories: set[str] = set()
    count = 0

    def visit(item: Any) -> Any:
        nonlocal count
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if _SECRET_KEY.search(key):
                    result[key] = REDACTED
                    categories.add("credential_fields")
                    count += 1
                else:
                    result[key] = visit(child)
            return result
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str):
            cleaned, changes = _redact_text(item, categories)
            count += changes
            return cleaned
        return item

    return visit(value), {"categories": sorted(categories), "count": count}
