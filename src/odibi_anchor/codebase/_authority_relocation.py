"""Verified restore-location attestations; never rewrite historical authority rows.

Only the staged, verified restore path may append these records. They are not a
public ownership-adoption API. Source targets and trust/project identities never move.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DOMAIN = "authority_relocation"
_DDL = (
    "CREATE TABLE anchor_home_relocations (attestation_id TEXT PRIMARY KEY, source_home TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL)",
    "CREATE TRIGGER anchor_home_relocations_no_update BEFORE UPDATE ON anchor_home_relocations BEGIN SELECT RAISE(ABORT,'relocation attestations are immutable'); END",
    "CREATE TRIGGER anchor_home_relocations_no_delete BEFORE DELETE ON anchor_home_relocations BEGIN SELECT RAISE(ABORT,'relocation attestations are immutable'); END",
)
_SCHEMA = hashlib.sha256((";\n".join(_DDL) + ";\n").encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RuntimeError("relocation requires absolute home paths")
    return os.path.normcase(str(Path(value).resolve()))


def _edges(relocations):
    edges = {}
    for event in relocations:
        source, destination = _path(event["source_home"]), _path(event["destination_home"])
        if source in edges:
            raise RuntimeError("ambiguous relocation fork")
        edges[source] = destination
    for source in edges:
        seen = set()
        while source in edges:
            if source in seen:
                raise RuntimeError("cyclic relocation chain")
            seen.add(source)
            source = edges[source]
    return edges


def rebase_identity(identity, relocations, current_home):
    """Resolve only a complete attested home chain; otherwise retain exact identity."""
    if not relocations or not identity.get("anchor_home") or not current_home:
        return identity
    edges = _edges(relocations)
    source = _path(identity["anchor_home"])
    cursor = source
    destination = _path(current_home)
    while cursor != destination and cursor in edges:
        cursor = edges[cursor]
    if cursor != destination or source == destination:
        return identity
    result = dict(identity)
    for key in ("anchor_home", "artifact_root", "project_root"):
        if identity.get(key):
            try:
                relative = Path(_path(identity[key])).relative_to(source)
            except ValueError:
                continue
            result[key] = str(Path(destination) / relative)
    return result


def load_relocations(connection):
    """Validate the optional domain before returning any owner-mapping evidence."""
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "anchor_schema_versions" in tables:
        version = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
    if "anchor_home_relocations" not in tables and version is None:
        return []
    if tuple(version or ()) != (1, _SCHEMA):
        raise RuntimeError("relocation schema version/checksum mismatch")
    query = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='anchor_home_relocations' AND sql IS NOT NULL ORDER BY type,name"
    with sqlite3.connect(":memory:") as expected:
        for statement in _DDL:
            expected.execute(statement)
        if [tuple(r) for r in connection.execute(query)] != expected.execute(query).fetchall():
            raise RuntimeError("relocation schema modified")
    authority = connection.execute("SELECT authority_id FROM anchor_authority_identity WHERE singleton=1").fetchone()
    records = []
    for row in connection.execute("SELECT attestation_id,source_home,event_json,event_sha256 FROM anchor_home_relocations"):
        event = json.loads(row[2])
        if (set(event) != {"source_home", "destination_home", "snapshot_id", "snapshot_sha256",
                          "logical_digest", "authority_id", "restored_at"}
                or _digest(event) != row[3] or row[0] != "ar_" + row[3]
                or event["source_home"] != row[1]
                or authority is None or event["authority_id"] != authority[0]
                or not all(re.fullmatch(r"[a-f0-9]{64}", event[k]) for k in ("snapshot_sha256", "logical_digest"))):
            raise RuntimeError("relocation attestation integrity mismatch")
        records.append(event)
    _edges(records)
    return records


def append_verified_relocation(path, *, source_home, destination_home, manifest):
    """Append only after restore has verified DB, bundle and continuity owners."""
    source_home, destination_home = _path(source_home), _path(destination_home)
    if source_home == destination_home:
        return None
    event = {"source_home": source_home, "destination_home": destination_home,
             "snapshot_id": manifest["snapshot_id"], "snapshot_sha256": manifest["sha256"],
             "logical_digest": manifest["logical_digest"], "authority_id": manifest["authority_id"],
             "restored_at": datetime.now(UTC).isoformat()}
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "anchor_schema_versions" not in tables:
            connection.execute("CREATE TABLE anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)")
        existing = load_relocations(connection)
        _edges([*existing, event])
        if "anchor_home_relocations" not in tables:
            for statement in _DDL:
                connection.execute(statement)
            connection.execute("INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                               (DOMAIN, 1, _SCHEMA, event["restored_at"]))
        checksum = _digest(event)
        connection.execute("INSERT INTO anchor_home_relocations VALUES(?,?,?,?)",
                           ("ar_" + checksum, source_home, _canonical(event), checksum))
        load_relocations(connection)
    return "ar_" + checksum
