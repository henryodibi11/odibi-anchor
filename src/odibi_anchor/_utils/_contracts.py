"""Durable data-contract persistence (.anchor_contracts/<subject>.json) — D-002.

A contract is the producer-owned, code-defined definition of "good data" for a
subject: its schema, candidate keys, freshness signal, and shape. Saving one lets a
future quality check auto-validate without the agent re-supplying keys — automation
over tribal knowledge.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

_CONTRACTS_DIRNAME = ".anchor_contracts"


def _safe_name(subject: str) -> str:
    """Filesystem-safe slug for a subject label."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", subject.strip()) or "dataframe"
    return slug[:120]


def contracts_dir(root: str) -> str:
    return os.path.join(root, _CONTRACTS_DIRNAME)


def contract_path(root: str, subject: str) -> str:
    return os.path.join(contracts_dir(root), _safe_name(subject) + ".json")


def save_contract(
    root: str,
    subject: str,
    contract_ctx: dict[str, Any],
    *,
    saved_at: str | None = None,
) -> str:
    """Persist the durable fields of a contract context. Returns the path.

    Stores only the stable contract surface (schema, keys, freshness, top-level
    metrics) — not samples or per-column histograms, which are point-in-time.
    """
    payload = {
        "subject": subject,
        "saved_at": saved_at or "",
        "schema": contract_ctx.get("schema", []),
        "candidate_keys": contract_ctx.get("candidate_keys", {}),
        "freshness": contract_ctx.get("freshness", {}),
        "metrics": {
            k: contract_ctx.get("metrics", {}).get(k)
            for k in ("row_count", "column_count")
        },
    }
    d = contracts_dir(root)
    os.makedirs(d, exist_ok=True)
    path = contract_path(root, subject)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return path


def load_contract(root: str, subject: str) -> dict[str, Any] | None:
    """Load a saved contract for subject, or None if absent/unreadable."""
    path = contract_path(root, subject)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def contract_keys(contract: dict[str, Any]) -> list[str]:
    """Extract the provided/candidate key columns from a saved contract."""
    ck = contract.get("candidate_keys", {})
    provided = ck.get("provided_key") or {}
    cols = provided.get("columns")
    if cols:
        return list(cols)
    inferred = ck.get("inferred_single_column_keys") or []
    return [inferred[0]] if inferred else []


def contract_schema_map(contract: dict[str, Any]) -> dict[str, str]:
    """{column: dtype} from a saved contract's schema list."""
    return {
        c["name"]: c.get("dtype", "")
        for c in contract.get("schema", [])
        if isinstance(c, dict) and "name" in c
    }
