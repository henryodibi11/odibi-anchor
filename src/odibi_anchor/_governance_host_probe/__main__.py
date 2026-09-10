"""Command-line entry point for passive governance host inspection."""

from __future__ import annotations

import json
import sys
from typing import Any

from . import PROBE_SCHEMA_VERSION
from ._capabilities import probe_host_capabilities


def _collection_failure_report() -> dict[str, Any]:
    """Return a fixed fail-closed report when passive inspection cannot complete."""
    return {
        "document_type": "governance_host_capability_probe",
        "schema_version": PROBE_SCHEMA_VERSION,
        "outcome": "unsupported",
        "authorizes_readiness": False,
        "probe_mode": "passive_read_only",
        "platform": {"system": "unavailable"},
        "cgroup": {
            "version": None,
            "current_relative_path": None,
            "controllers": [],
            "subtree_control_writable": False,
            "current_cgroup_writable": False,
            "cgroup_kill_present": False,
            "active_child_creation_qualified": False,
        },
        "process_identity": {
            "pidfd_open_available": False,
            "clone3_into_cgroup_qualified": False,
        },
        "trusted_host_interfaces": {
            "amp_inventory": "unavailable",
            "gateway_revocation": "unavailable",
            "checkout_creation_identity": "unavailable",
        },
        "blockers": [
            "clone3_into_cgroup_unqualified",
            "gateway_revocation_unavailable",
            "host_capability_collection_failed",
            "host_checkout_creation_identity_unavailable",
            "trusted_amp_inventory_unavailable",
        ],
        "limitations": [
            "Passive host inspection failed before complete observations were available.",
            "This report is not a readiness receipt, fence qualification, or authorization input.",
            "Trusted Amp inventory, gateway revocation, and checkout identity require an external host authority.",
        ],
    }


def main() -> int:
    try:
        report = probe_host_capabilities()
        output = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (OSError, TypeError, ValueError):
        output = json.dumps(_collection_failure_report(), separators=(",", ":"), sort_keys=True)
    sys.stdout.write(output + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
