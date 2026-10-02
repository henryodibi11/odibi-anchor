"""Internal verified-restore receipts for filesystem-local draft observations.

Workflow history is immutable. Snapshot proofs establish that the local baseline
still held before and after packing; only verified restore may append a replacement
local observation. Neither a content match nor a timestamp alone grants authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from odibi_anchor.codebase._authority_relocation import load_relocations, rebase_identity
from odibi_anchor.codebase._workflow import (
    WorkflowError,
    _connection,
    _events,
    _schema,
    canonical,
    digest,
)

DOMAIN = "workflow_artifact_restore"
_DDL = (
    "CREATE TABLE workflow_artifact_restores (receipt_sha256 TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)",
    "CREATE TRIGGER workflow_artifact_restores_no_update BEFORE UPDATE ON workflow_artifact_restores BEGIN SELECT RAISE(ABORT,'artifact restore receipts are immutable'); END",
    "CREATE TRIGGER workflow_artifact_restores_no_delete BEFORE DELETE ON workflow_artifact_restores BEGIN SELECT RAISE(ABORT,'artifact restore receipts are immutable'); END",
)
_SCHEMA = hashlib.sha256((";\n".join(_DDL) + ";\n").encode()).hexdigest()


def load_restores(connection):
    """Validate the optional append-only domain before consuming any receipt."""
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = (connection.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
    ).fetchone() if "anchor_schema_versions" in tables else None)
    if "workflow_artifact_restores" not in tables and version is None:
        return []
    if tuple(version or ()) != (1, _SCHEMA):
        raise WorkflowError("integrity", "artifact restore schema version/checksum mismatch")
    query = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='workflow_artifact_restores' AND sql IS NOT NULL ORDER BY type,name"
    with sqlite3.connect(":memory:") as expected:
        for statement in _DDL:
            expected.execute(statement)
        if [tuple(r) for r in connection.execute(query)] != expected.execute(query).fetchall():
            raise WorkflowError("integrity", "artifact restore schema modified")
    authority = connection.execute("SELECT authority_id FROM anchor_authority_identity WHERE singleton=1").fetchone()
    receipts = []
    for checksum, encoded in connection.execute("SELECT receipt_sha256,receipt_json FROM workflow_artifact_restores"):
        receipt = json.loads(encoded)
        if (canonical(receipt) != encoded or digest(receipt) != checksum
                or set(receipt) != {"proof", "observation", "owner", "authority_id", "manifest_sha256"}
                or authority is None or receipt["authority_id"] != authority[0]):
            raise WorkflowError("integrity", "artifact restore receipt integrity mismatch")
        receipts.append(receipt)
    return receipts


def _matches(state, owner, observed, receipts):
    baseline = state.get("artifact_baseline")
    if not baseline or baseline.get("plan_sha256") != state["plan_sha256"]:
        return False
    if observed == baseline:
        return True
    return any(receipt["owner"] == owner and receipt["observation"] == observed
               and receipt["proof"]["baseline_sha256"] == digest(baseline)
               and receipt["proof"]["workflow_id"] == state["workflow_id"] for receipt in receipts)


def matches_restored_baseline(path, *, state, owner, observed, historical=False):
    """Verify exact observations; historical admission may follow attested relocation."""
    with _connection(path) as connection:
        receipts = load_restores(connection)
        if historical:
            relocations = load_relocations(connection)
            receipts = [{**receipt, "owner": rebase_identity(receipt["owner"], relocations, owner["anchor_home"])}
                        for receipt in receipts]
        return _matches(state, owner, observed, receipts)


def _drafts(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "workflow_events" not in tables:
        return []
    _schema(connection)
    states = []
    for identifier, in connection.execute("SELECT DISTINCT workflow_id FROM workflow_events ORDER BY workflow_id"):
        row = connection.execute(
            "SELECT event_json FROM workflow_events WHERE workflow_id=? ORDER BY generation LIMIT 1", (identifier,),
        ).fetchone()
        owner = json.loads(row[0])["state"]["owner"]
        state = _events(connection, identifier, owner)[-1]["state"]
        if state["progress"] == "draft" and state["plan"]["execution_mode"] == "artifact_only":
            states.append(state)
    return states


def _local_owner(state, relocations, projects):
    owner = rebase_identity(state["owner"], relocations, str(projects.parent.parent))
    if Path(owner["artifact_root"]) != projects / owner["project_id"]:
        raise WorkflowError("wrong_authority", "artifact restore requires exact project-root authority")
    return owner


def snapshot_proofs(path, projects):
    """Observe only baseline-consistent drafts; drift remains recoverable but unadmitted."""
    from odibi_anchor._dispatcher._workflow_evidence import collect_artifact_baseline

    proofs = []
    with _connection(path) as connection:
        receipts = load_restores(connection)
        relocations = load_relocations(connection)
        for state in _drafts(connection):
            if not state.get("artifact_baseline"):
                continue
            try:
                owner = _local_owner(state, relocations, projects)
                observed = collect_artifact_baseline(state["plan"], session_state=SimpleNamespace(**owner))
            except WorkflowError as exc:
                if exc.code not in {"wrong_authority", "out_of_scope", "unavailable", "stale_evidence"}:
                    raise
                continue
            if _matches(state, owner, observed, receipts):
                proofs.append({"workflow_id": state["workflow_id"], "state_sha256": digest(state),
                               "baseline_sha256": digest(state["artifact_baseline"]),
                               "owner": owner, "observation": observed})
    return proofs


def _content(observation):
    return {name: None if value is None else {"sha256": value["sha256"], "size": value["size"]}
            for name, value in observation["files"].items()}


def append_verified_restores(path, *, projects, manifest):
    """Called only after verified extraction/copy, before publishing the restored DB."""
    from odibi_anchor._dispatcher._workflow_evidence import collect_artifact_baseline

    proofs = manifest["artifacts"].get("workflow_baselines", [])
    if not isinstance(proofs, list):
        raise WorkflowError("integrity", "artifact restore proofs must be a list")
    if not proofs:
        return
    with _connection(path, write=True) as connection:
        receipts = load_restores(connection)
        relocations = load_relocations(connection)
        states = {state["workflow_id"]: state for state in _drafts(connection)}
        pending = []
        for proof in proofs:
            state = states.get(proof.get("workflow_id")) if isinstance(proof, dict) else None
            if (state is None or set(proof) != {"workflow_id", "state_sha256", "baseline_sha256", "owner", "observation"}
                    or proof["state_sha256"] != digest(state)
                    or proof["baseline_sha256"] != digest(state.get("artifact_baseline"))
                    or rebase_identity(state["owner"], relocations, proof["owner"]["anchor_home"]) != proof["owner"]
                    or not _matches(state, proof["owner"], proof["observation"], receipts)):
                raise WorkflowError("integrity", "artifact restore proof does not match draft authority")
            owner = _local_owner(state, relocations, projects)
            observed = collect_artifact_baseline(state["plan"], session_state=SimpleNamespace(**owner))
            if _content(observed) != _content(proof["observation"]):
                raise WorkflowError("integrity", "artifact restore content differs from verified baseline")
            # The manifest retains the full source observation. Bind its digest
            # here instead of doubling per-file metadata within the packet limit.
            retained_proof = {key: value for key, value in proof.items() if key != "observation"}
            retained_proof["observation_sha256"] = digest(proof["observation"])
            pending.append({"proof": retained_proof, "owner": owner, "observation": observed,
                            "authority_id": manifest["authority_id"],
                            "manifest_sha256": manifest["manifest_sha256"]})
        if not connection.execute("SELECT 1 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)).fetchone():
            for statement in _DDL:
                connection.execute(statement)
            connection.execute("INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                               (DOMAIN, 1, _SCHEMA, datetime.now(UTC).isoformat()))
        for receipt in pending:
            connection.execute("INSERT INTO workflow_artifact_restores VALUES(?,?)",
                               (digest(receipt), canonical(receipt)))
        load_restores(connection)
