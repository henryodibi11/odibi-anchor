"""Native skill discovery, loading, distribution, and routing contracts."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._guidance import (
    NATIVE_SKILLS,
    GuidanceTarget,
    guidance_distribution_manifest,
    guidance_registry_metadata,
    resolve_and_load_guidance,
    select_guidance,
)
from odibi_anchor.planning._task_policy import BpsKernel, TaskPolicyContext
from odibi_anchor.planning._task_profile import normalize_task_profile

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".assistant" / "skills"
EXPECTED = (
    "auditing-memory-governance", "authoring-governed-memories", "building-memory-packs",
    "code-comprehension", "cross-functional-pr", "data-onboarding",
    "data-operations", "data-reconciliation", "debugging",
    "dependency-management", "documentation", "incident-response",
    "performance-investigation", "schema-design", "setting-up-odibi-anchor", "work-item-management",
    "writing-specs", "writing-tests",
)


def _context(*, mode="planning", action="task", effect="orient", **profile):
    from odibi_anchor._dispatcher._effects import EFFECT_PERMISSIONS
    normalized = normalize_task_profile(legacy_mode=mode, **profile)
    return TaskPolicyContext(
        normalized, BpsKernel("task", "outcome"), current_action=action,
        current_effect=effect,
        current_effect_permissions=EFFECT_PERMISSIONS[effect],
    )


def test_exact_native_registry_metadata_and_direct_resolution():
    assert NATIVE_SKILLS == EXPECTED
    assert tuple(sorted(path.name for path in SKILLS.iterdir() if (path / "SKILL.md").is_file())) == tuple(sorted(EXPECTED))
    metadata = guidance_registry_metadata(SKILLS)
    assert metadata == tuple({
        "name": name,
        "path": f"skills/{name}/SKILL.md",
        "guidance_target": {"skill": name},
    } for name in EXPECTED)
    for name in EXPECTED:
        target, content, paths = resolve_and_load_guidance(SKILLS, name)
        assert target == GuidanceTarget(name)
        assert content
        assert paths == (SKILLS / name / "SKILL.md",)


@pytest.mark.parametrize("name", [
    "planning", "planning-delivery", "data-engineering",
    "writing-tests/references/testing-standards.md", "../writing-tests",
])
def test_removed_and_nested_names_are_unknown(name):
    with pytest.raises(ValueError, match=r"^Unknown skill .*available skills:"):
        resolve_and_load_guidance(SKILLS, name)


@pytest.mark.parametrize(("profile", "expected"), [
    ({"traits": ["memory-governance"]}, ("auditing-memory-governance",)),
    ({"traits": ["memory-authoring"]}, ("authoring-governed-memories",)),
    ({"traits": ["memory-packaging"]}, ("building-memory-packs",)),
    ({"mode": "analysis", "domains": ["code"]}, ("code-comprehension",)),
    ({"mode": "review", "traits": ["pr"]}, ("code-comprehension", "cross-functional-pr")),
    ({"traits": ["ingestion"]}, ("data-onboarding",)),
    ({"execution_mode": "data_change"}, ("data-operations", "writing-specs")),
    ({"execution_mode": "source_change", "traits": ["source-change", "data-change"]}, ("data-operations",)),
    ({"traits": ["reconciliation"]}, ("data-reconciliation",)),
    ({"mode": "debugging"}, ("debugging",)),
    ({"traits": ["dependency"]}, ("dependency-management",)),
    ({"mode": "documentation"}, ("documentation",)),
    ({"traits": ["incident"]}, ("incident-response",)),
    ({"traits": ["performance"]}, ("performance-investigation",)),
    ({"traits": ["schema"]}, ("schema-design",)),
    ({"traits": ["work-item"]}, ("work-item-management",)),
    ({"mode": "spec_creation"}, ("writing-specs",)),
    ({"mode": "testing", "traits": ["test-authoring"]}, ("writing-tests",)),
])
def test_positive_routing_for_every_native_owner(profile, expected):
    assert tuple(target.skill for target in select_guidance(_context(**profile))) == expected


def test_negative_and_composition_routing_has_no_generic_owner():
    assert select_guidance(_context()) == ()
    assert tuple(target.skill for target in select_guidance(_context(
        traits=["reconciliation", "mutation", "test-authoring"],
        execution_mode="data_change",
    ))) == ("data-operations", "data-reconciliation", "writing-specs")


@pytest.mark.parametrize(("owner", "profile"), [
    ("auditing-memory-governance", {"mode": "implementation", "domains": ["code"]}),
    ("authoring-governed-memories", {"traits": ["memory-governance"]}),
    ("building-memory-packs", {"traits": ["memory-authoring"]}),
    ("code-comprehension", {"mode": "analysis", "domains": ["data"]}),
    ("cross-functional-pr", {"mode": "review", "traits": ["review"]}),
    ("data-onboarding", {"execution_mode": "data_change"}),
    ("data-operations", {"traits": ["reconciliation"]}),
    ("data-reconciliation", {"execution_mode": "data_change"}),
    ("debugging", {"traits": ["performance"]}),
    ("dependency-management", {"mode": "implementation", "domains": ["code"]}),
    ("documentation", {"mode": "analysis", "traits": ["comprehension"]}),
    ("incident-response", {"mode": "debugging"}),
    ("performance-investigation", {"mode": "debugging"}),
    ("schema-design", {"traits": ["onboarding"]}),
    ("work-item-management", {"mode": "planning"}),
    ("writing-specs", {"mode": "testing"}),
    ("writing-tests", {"mode": "testing"}),
])
def test_each_native_owner_has_a_negative_routing_boundary(owner, profile):
    selected = {target.skill for target in select_guidance(_context(**profile))}
    assert owner not in selected


@pytest.mark.parametrize(("profile", "expected"), [
    (
        {"traits": ["onboarding", "schema"]},
        ("data-onboarding", "schema-design"),
    ),
    (
        {"mode": "review", "domains": ["code"], "traits": ["pr"]},
        ("code-comprehension", "cross-functional-pr"),
    ),
    (
        {"traits": ["incident", "performance"]},
        ("incident-response", "performance-investigation"),
    ),
    (
        {"mode": "testing", "traits": ["test-authoring", "documentation"]},
        ("writing-tests",),
    ),
])
def test_composed_routing_selects_only_material_owners(profile, expected):
    assert tuple(target.skill for target in select_guidance(_context(**profile))) == expected


@pytest.mark.parametrize("invalid", ["symlink", "crc", "cache", "non_utf8"])
def test_distribution_manifest_fails_closed(tmp_path, invalid):
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    target = root / ".assistant" / "skills" / EXPECTED[0]
    if invalid == "symlink":
        try:
            (target / "unsafe").symlink_to(target / "SKILL.md")
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("Windows symlink privilege is unavailable")
            raise
    elif invalid == "crc":
        (target / "unsafe.crc").write_text("cache", encoding="utf-8")
    elif invalid == "cache":
        (target / "__pycache__").mkdir()
        (target / "__pycache__" / "unsafe.pyc").write_bytes(b"cache")
    else:
        (target / "unsafe.bin").write_bytes(b"\xff")
    with pytest.raises(ValueError):
        guidance_distribution_manifest(root)


def test_distribution_manifest_is_deterministic_and_complete():
    first = guidance_distribution_manifest(ROOT)
    second = guidance_distribution_manifest(ROOT)
    assert first == second
    paths = [entry.relative_path for entry in first["files"]]
    assert ".assistant/references/reviewing/independent-pr-review.md" in paths
    expected = sorted(
        (path.relative_to(ROOT).as_posix() for path in (ROOT / ".assistant").rglob("*") if path.is_file()),
        key=lambda path: path.encode("utf-8"),
    )
    assert paths == expected
    assert first["file_count"] == len(expected)
    assert first["directory_count"] == sum(path.is_dir() for path in (ROOT / ".assistant").rglob("*")) + 1
    assert first["byte_count"] == sum((ROOT / path).stat().st_size for path in expected)
    assert first["instruction_byte_count"] == (ROOT / ".assistant_instructions.md").stat().st_size
    with pytest.raises(TypeError):
        first["file_count"] = 0


def test_distribution_manifest_ignores_only_generated_assistant_launcher_cache(tmp_path):
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    expected = guidance_distribution_manifest(root)
    cache = root / ".assistant" / "__pycache__"
    cache.mkdir()
    (cache / "agent_bootstrap.cpython-311.pyc").write_bytes(b"generated cache")

    assert guidance_distribution_manifest(root) == expected

    (cache / "unrelated.cpython-311.pyc").write_bytes(b"unexpected cache")
    with pytest.raises(ValueError, match="unexpected file"):
        guidance_distribution_manifest(root)


@pytest.mark.parametrize("race", ["replace-directory", "add-file"])
def test_distribution_manifest_rejects_directory_races(tmp_path, monkeypatch, race):
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    real_listdir = os.listdir
    fired = False

    def racing_listdir(path):
        nonlocal fired
        entries = list(real_listdir(path))
        descriptor_path = Path(f"/proc/self/fd/{path}").resolve() if isinstance(path, int) else Path(path)
        if not fired and descriptor_path == root / ".assistant":
            fired = True
            if race == "add-file":
                (descriptor_path / "raced.md").write_text("race", encoding="utf-8")
            else:
                moved = root / "assistant-old"
                descriptor_path.rename(moved)
                (root / ".assistant").mkdir()
                (root / ".assistant" / "replacement.md").write_text("race", encoding="utf-8")
        return entries

    monkeypatch.setattr(os, "listdir", racing_listdir)
    with pytest.raises(ValueError, match="directory changed while scanning"):
        guidance_distribution_manifest(root)


def test_distribution_manifest_rejects_file_swap_between_enumeration_and_open(tmp_path, monkeypatch):
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    target = root / ".assistant" / "README.md"
    replacement = root / "replacement.md"
    replacement.write_text("replacement", encoding="utf-8")
    real_open = os.open
    fired = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal fired
        descriptor_target = path == "README.md" and kwargs.get("dir_fd") is not None
        path_target = kwargs.get("dir_fd") is None and Path(path) == target
        if not fired and (descriptor_target or path_target):
            fired = True
            target.unlink()
            if os.name == "nt":
                replacement.replace(target)
            else:
                target.symlink_to(replacement)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(
        ValueError,
        match=r"could not be read|changed while reading|symlink is not allowed",
    ):
        guidance_distribution_manifest(root)


def test_distribution_manifest_rejects_resource_root_replacement(tmp_path, monkeypatch):
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    real_open = os.open
    fired = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal fired
        descriptor_target = path == ".assistant_instructions.md" and kwargs.get("dir_fd") is not None
        path_target = kwargs.get("dir_fd") is None and Path(path) == root / ".assistant_instructions.md"
        if not fired and (descriptor_target or path_target):
            fired = True
            root.rename(tmp_path / "resources-old")
            root.mkdir()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(
        ValueError, match=r"resource root changed while scanning|resource could not be read",
    ):
        guidance_distribution_manifest(root)


def test_distribution_manifest_rejects_in_place_write_with_restored_mtime(tmp_path, monkeypatch):
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    target = root / ".assistant" / "large.md"
    target.write_bytes(b"a" * (2 * 1024 * 1024))
    original = target.stat()
    real_read = os.read
    real_open = os.open
    fired = False
    target_descriptor = None

    def tracking_open(path, flags, *args, **kwargs):
        nonlocal target_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is None and Path(path) == target:
            target_descriptor = descriptor
        return descriptor

    def racing_read(file_descriptor, count):
        nonlocal fired
        content = real_read(file_descriptor, count)
        descriptor_path = (
            Path(f"/proc/self/fd/{file_descriptor}").resolve() if os.name != "nt" else None
        )
        if not fired and (descriptor_path == target or file_descriptor == target_descriptor):
            fired = True
            with target.open("r+b") as stream:
                stream.seek(1024 * 1024 + 100)
                stream.write(b"b" * 100)
            deadline = time.monotonic() + 1
            while target.stat().st_ctime_ns == original.st_ctime_ns:
                if time.monotonic() >= deadline:
                    raise AssertionError("filesystem ctime did not advance after an in-place write")
                time.sleep(0.001)
                with target.open("r+b") as stream:
                    stream.seek(1024 * 1024 + 100)
                    stream.write(b"b" * 100)
            os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        return content

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "read", racing_read)
    with pytest.raises(ValueError, match="resource changed while reading"):
        guidance_distribution_manifest(root)
