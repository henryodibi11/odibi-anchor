"""Compact Markdown rendering for operational evidence returned through anchor()."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def render_operational_markdown(action: str, value: Mapping[str, Any]) -> str:
    """Render evidence without hiding status, limitations, or unavailable channels."""
    title = action.replace("_", " ").title()
    lines = [f"# {title}", ""]
    if value.get("status"):
        lines.append(f"- **Status:** {value['status']}")
    if value.get("observed_at"):
        lines.append(f"- **Observed:** {value['observed_at']}")
    if value.get("incident_id"):
        lines.append(f"- **Incident:** `{value['incident_id']}`")
    facts = value.get("facts")
    if facts:
        lines.extend(["", "## Facts", "", "```json", json.dumps(facts, indent=2, ensure_ascii=False), "```"])
    findings = value.get("findings") or []
    if findings:
        lines.extend(["", "## Findings", ""])
        lines.extend(f"- {item.get('statement', item)}" if isinstance(item, Mapping) else f"- {item}" for item in findings)
    limitations = value.get("limitations") or value.get("synthesis", {}).get("limitations", [])
    if limitations:
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in limitations)
    if not facts and not findings and not limitations:
        lines.extend(["", "```json", json.dumps(value, indent=2, ensure_ascii=False), "```"])
    return "\n".join(lines) + "\n"
