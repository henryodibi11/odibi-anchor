"""Canonical learning guidance exposes only structured learning and governed promotion."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_LEGACY_TOKENS = (
    'anchor("learn"',
    "anchor('learn'",
    'anchor("confirm"',
    "anchor('confirm'",
    "learn_context",
    "confirm_memory",
    "learn_events",
)


def test_active_guidance_has_no_removed_public_legacy_api():
    failures = []
    paths = [
        ROOT / ".assistant_instructions.md",
        *sorted((ROOT / ".assistant").rglob("*.md")),
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "src" / "odibi_anchor").rglob("*.md")),
    ]
    for path in paths:
        if ".assistant/references/snapshots/" in path.as_posix():
            continue
        content = path.read_text(encoding="utf-8").lower()
        for token in PUBLIC_LEGACY_TOKENS:
            if token in content:
                failures.append(f"{path.relative_to(ROOT)}:{token}")
    assert failures == []


def test_public_python_and_dispatch_surfaces_omit_removed_legacy_api():
    import odibi_anchor.codebase as codebase
    from odibi_anchor._dispatcher._dispatch_table import ACTION_GROUPS, DISPATCH_SIGS
    from odibi_anchor._dispatcher._effects import BUILTIN_ACTION_NAMES

    for action in ("learn", "confirm"):
        assert action not in BUILTIN_ACTION_NAMES
        assert action not in DISPATCH_SIGS
        assert all(action not in members for members in ACTION_GROUPS.values())
    for exported in ("learn_context", "render_learn_report", "confirm_memory"):
        assert exported not in codebase.__all__
        assert not hasattr(codebase, exported)


def test_canonical_memory_guidance_names_governed_lanes_and_denies_organic_promotion():
    paths = (
        ROOT / ".assistant" / "skills" / "authoring-governed-memories" / "SKILL.md",
        ROOT / ".assistant" / "skills" / "auditing-memory-governance" / "SKILL.md",
        ROOT / ".assistant" / "references" / "odibi-anchor" / "actions.md",
    )
    contents = [path.read_text(encoding="utf-8").lower() for path in paths]
    for path, content in zip(paths, contents, strict=True):
        assert "request_owner_activation" in content, path
        assert "request_owner_confirmation" in content, path
        assert "verifier" in content, path
        assert "slack" in content and "local" in content and "windows" in content, path
        assert "databricks" in content and "lower-assurance" in content, path
    authoring, auditing, actions = contents
    for content in (authoring, auditing, actions):
        assert "application" in content and "evaluation" in content and "counters" in content
        promotion_contexts = [
            line for line in content.splitlines()
            if "counter" in line or "application" in line or "evaluation" in line
        ]
        assert any("never" in line or "none" in line for line in promotion_contexts)
    dispatch_help = (
        ROOT / "src" / "odibi_anchor" / "_dispatcher" / "_dispatch_table.py"
    ).read_text(encoding="utf-8").lower()
    assert "request_owner_activation" in dispatch_help
    assert "request_owner_confirmation" in dispatch_help
    from odibi_anchor._dispatcher._dispatch_table import build_help_text

    rendered_help = build_help_text("memory")
    assert "request_owner_activation" in rendered_help
    assert "request_owner_confirmation" in rendered_help
    for root in (ROOT / ".assistant", ROOT / "docs", ROOT / "src" / "odibi_anchor"):
        for path in root.rglob("*"):
            if path.suffix in {".md", ".py"}:
                assert "runtime promotion is unavailable" not in path.read_text(
                    encoding="utf-8"
                ).lower(), path


def test_canonical_memory_guidance_requires_overlap_scope_and_lineage_checks():
    authoring = (
        ROOT / ".assistant" / "skills" / "authoring-governed-memories" / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    auditing = (
        ROOT / ".assistant" / "skills" / "auditing-memory-governance" / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    standards = (
        ROOT / ".assistant" / "references" / "odibi-anchor" / "capture-standards.md"
    ).read_text(encoding="utf-8").lower()

    for content in (authoring, auditing, standards):
        assert "active project" in content
        assert "trust domain" in content
        assert "equivalent" in content
        assert "provenance" in content
        assert "cross-project" in content
        assert "independent" in content
    assert "no supported public attachment" in authoring
    assert "never consolidate" in auditing and "across trust domains" in auditing
    assert "materially distinct or contradictory claims remain separate" in standards
