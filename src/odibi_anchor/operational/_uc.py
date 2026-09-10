"""Unity Catalog metadata normalization without effective-privilege claims."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._contract import CollectorResult, ContractError, normalize_json
from ._redaction import redact


def _location(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        value = urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, "", ""))
    except ValueError:
        pass
    return redact(value)[0]


def uc_context(metadata: Mapping[str, Any]) -> CollectorResult:
    """Normalize an adapter-produced UC metadata mapping.

    ``status`` may be collected/unavailable/denied/failed, allowing an adapter to
    preserve channel outcomes without throwing away the snapshot.
    """
    if not isinstance(metadata, Mapping):
        raise ContractError("metadata must be a mapping")
    status = metadata.get("status", "collected")
    if status not in ("collected", "unavailable", "denied", "failed"):
        raise ContractError("invalid UC status")
    source = metadata.get("source", {"provider": "databricks", "channel": "injected"})
    limitations = list(metadata.get("limitations", []))
    limitations.append("direct grants do not establish effective privileges; group and inherited grants were not resolved")
    fields = ("object", "detail", "direct_grants", "tags", "dependencies", "table_lineage",
              "column_lineage", "row_filters", "column_masks", "execution_principal")
    # Absence means the channel was not queried, not that it was queried and empty.
    facts = {field: metadata.get(field) for field in fields}
    detail = facts.get("detail")
    if isinstance(detail, Mapping):
        detail = dict(detail)
        detail["location"] = _location(detail.get("location"))
        facts["detail"] = detail
    cleaned, summary = redact({"source": source, "facts": facts, "limitations": limitations,
                               "error": metadata.get("error")})
    findings = ()
    if status == "collected":
        findings = ({"kind": "direct_grants", "statement": "Direct grants were normalized without inferring effective privileges.",
                     "evidence": {"channel": "direct_grants", "count": len(facts["direct_grants"]) if isinstance(facts["direct_grants"], list) else 0}},)
    return CollectorResult(collector="uc_context", status=status, source=cleaned["source"],
                           environment=normalize_json(metadata.get("environment", {})), facts=cleaned["facts"],
                           findings=findings, limitations=tuple(cleaned["limitations"]),
                           error=cleaned["error"], redaction=summary)
