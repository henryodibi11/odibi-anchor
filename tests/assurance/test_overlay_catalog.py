"""Closed overlay catalog, provenance, and version identity tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from odibi_anchor.assurance import (
    CATALOG_VERSION,
    CONTROL_CATALOG,
    CORE_CONTROL_CATALOG,
    OVERLAY_CATALOG_VERSION,
    OVERLAY_CONTROLS,
    OVERLAYS,
    SOURCES,
    catalog_digest,
    core_catalog_digest,
    overlay_catalog_digest,
    validate_overlay_catalog,
)

ROOT = Path(__file__).resolve().parents[2]


def test_catalogs_have_deliberate_versions_closed_counts_and_digests():
    assert CATALOG_VERSION == "anchor-assurance-core/1.1"
    assert OVERLAY_CATALOG_VERSION == "1.0.0"
    assert len(CORE_CONTROL_CATALOG) == 11
    assert len(OVERLAYS) == 8
    assert len(OVERLAY_CONTROLS) == 24
    assert len(CONTROL_CATALOG) == 35
    assert core_catalog_digest() == "a64fad1c5f7fd1834daf1b087667191f4ec0a15e2e7066ce66016fba6d2c1e0e"
    assert overlay_catalog_digest() == "0277bea95ab65a69255794fe9e4d651a3fcf95548b6b980aa63f93de7ccc473b"
    assert catalog_digest() == "f58e6ca37d29134e0ace3f94b96315746aa1fd324a1cd120e5f5da3e0cba5d8c"
    assert len({item.control_id for item in CONTROL_CATALOG}) == 35


def test_overlay_catalog_is_immutable_and_structurally_validated():
    validate_overlay_catalog()
    with pytest.raises(FrozenInstanceError):
        OVERLAYS[0].title = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="duplicate overlay control"):
        validate_overlay_catalog(controls=(*OVERLAY_CONTROLS, OVERLAY_CONTROLS[0]))
    bad = replace(OVERLAYS[0], source_ids=("missing.source",))
    with pytest.raises(ValueError, match="unknown source"):
        validate_overlay_catalog(overlays=(bad, *OVERLAYS[1:]))
    with pytest.raises(ValueError, match="non-empty sorted tuples"):
        replace(OVERLAY_CONTROLS[0], required_evidence=())


def test_all_sources_retain_version_license_capture_and_refresh_metadata():
    assert len(SOURCES) == 15
    assert len({item.source_id for item in SOURCES}) == len(SOURCES)
    for source in SOURCES:
        assert source.baseline and source.url.startswith("https://")
        assert source.license_boundary and source.captured_on == "2026-08-20"
        assert source.refresh_triggers
    restricted = {"iso.25012", "iec.fmea", "iec.hazop"}
    assert all(not hasattr(source, "content_path") for source in SOURCES if source.source_id in restricted)


def test_authored_references_report_actual_canonical_catalog_identities():
    kernel = (ROOT / ".assistant/references/development/assurance-kernel.md").read_text()
    overlays = (ROOT / ".assistant/references/assurance/standards-overlays.md").read_text()
    assert CATALOG_VERSION in kernel
    assert core_catalog_digest() in kernel
    assert catalog_digest() in kernel and catalog_digest() in overlays
    assert OVERLAY_CATALOG_VERSION in overlays
    assert overlay_catalog_digest() in overlays
    forbidden = ("certified", "compliant with", "conforms to", "slsa level")
    authored = "\n".join(item.intent for item in OVERLAY_CONTROLS).lower() + overlays.lower()
    assert not any(claim in authored for claim in forbidden)
