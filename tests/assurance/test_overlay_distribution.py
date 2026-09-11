"""Reference routing and source/installed overlay identity tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from odibi_anchor._dispatcher._references import (
    load_reference_registry,
    task_reference_guidance,
)
from odibi_anchor.assurance import (
    OverlayInput,
    catalog_digest,
    core_catalog_digest,
    overlay_catalog_digest,
    select_overlays,
)

ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ROOT / ".assistant/references"


def test_registry_has_complete_compact_overlay_metadata_and_no_bundled_paid_source():
    registry = load_reference_registry(REFERENCES)
    entry = next(item for item in registry["entries"] if item["id"] == "assurance.standards-overlays")
    assert entry["path"] == "assurance/standards-overlays.md"
    assert entry["verification"] == ["documented"]
    assert len(entry["sources"]) == 15
    assert all(source["license"] and source["url"].startswith("https://") for source in entry["sources"])
    assert entry["offline_sources"] == ["assurance/standards-overlays.md"]


def test_compact_reference_routes_only_from_structured_selection():
    prose_guidance = task_reference_guidance(REFERENCES, "security data quality WCAG SLSA")
    assert "assurance.standards-overlays" not in {item["id"] for item in prose_guidance}
    selection = select_overlays(OverlayInput(
        "change", "source_change", "T2", domains=frozenset({"data"}),
    ))
    guidance = task_reference_guidance(
        REFERENCES, "unrelated words", overlay_selection=selection,
    )
    assert [item["id"] for item in guidance] == ["assurance.standards-overlays"]
    assert guidance[0]["selection"] == [{
        "overlay_id": "data.quality", "version": "1.0.0", "reason_codes": ["domain:data"],
    }]
    assert "content" not in guidance[0]


def test_native_skill_set_remains_exactly_eighteen_and_existing_pointers_are_narrow():
    skills = sorted((ROOT / ".assistant/skills").glob("*/SKILL.md"))
    assert len(skills) == 18
    pointer_paths = {
        ".assistant/skills/data-onboarding/references/quality.md",
        ".assistant/skills/data-operations/references/backbone.md",
        ".assistant/skills/dependency-management/references/backbone.md",
        ".assistant/skills/documentation/references/backbone.md",
        ".assistant/skills/incident-response/references/backbone.md",
    }
    for relative in pointer_paths:
        assert "assurance/standards-overlays.md" in (ROOT / relative).read_text(encoding="utf-8")


def test_wheel_contains_identical_registry_reference_and_catalog_identities(tmp_path):
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), "."],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for relative in ("registry.json", "assurance/standards-overlays.md"):
            source = (REFERENCES / relative).read_bytes()
            installed = archive.read(f".assistant/references/{relative}")
            assert installed == source
            assert hashlib.sha256(installed).digest() == hashlib.sha256(source).digest()
    script = (
        "import json; from odibi_anchor.assurance import "
        "catalog_digest,core_catalog_digest,overlay_catalog_digest; "
        "print(json.dumps([core_catalog_digest(),overlay_catalog_digest(),catalog_digest()]))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys;sys.path.insert(0,{str(wheel)!r});{script}"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    assert json.loads(completed.stdout) == [
        core_catalog_digest(), overlay_catalog_digest(), catalog_digest(),
    ]
