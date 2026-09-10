"""Canonical learning guidance must not regress to normal legacy closure advice."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED_ALTERNATIVES = (
    "genuine legacy content",
    "genuine legacy memory event",
    "or legacy learn",
    "legacy learning alternative",
    "legacy learn alternative",
)
OPERATIONAL_CONFIRMATION_PHRASES = (
    "confirm a pending memory",
    "confirm a memory helped",
    "confirm existing memory",
    "marks as verified",
    "boosts retrieval priority",
    "increase trust",
)
LEGACY_LEARNING_ROUTE_PHRASES = (
    "auto-record learnings at session end",
    "auto-record learnings",
    "session learning loop",
    "session learning:",
    "end-of-session learning",
    "end-of-session persist",
    "analyze session events and auto-record",
    "session-end batch pattern",
)


def _legacy_call(value: str) -> bool:
    compact = "".join(value.lower().split())
    return 'anchor("learn"' in compact or "anchor('learn'" in compact


def _valid_compatibility_context(value: str) -> bool:
    lowered = value.lower()
    return (
        "compatibility-only" in lowered
        and ("historical" in lowered or "old persisted" in lowered)
        and ("requir" in lowered or "technically" in lowered)
    )


def _confirm_call(value: str) -> bool:
    compact = "".join(value.lower().split())
    return 'anchor("confirm"' in compact or "anchor('confirm'" in compact


def _truthful_blocked_confirmation_context(value: str) -> bool:
    lowered = value.lower()
    return (
        ("compatibility" in lowered or "legacy" in lowered)
        and ("confirmation_blocked" in lowered or "blocked" in lowered)
        and ("governed" in lowered or "owner" in lowered or "verifier" in lowered)
    )


def _legacy_learning_route(value: str) -> bool:
    lowered = value.lower()
    return any(phrase in lowered for phrase in LEGACY_LEARNING_ROUTE_PHRASES)


def _operational_learn_context_label(value: str) -> bool:
    lowered = value.lower()
    return "learn_context" in lowered and any(
        marker in lowered
        for marker in ("learning", "auto-record", "session end", "session-end", "persist", " api")
    )


def test_user_facing_guidance_labels_every_legacy_learn_example_compatibility_only():
    """Scan tracked active guidance and imperative runtime strings semantically."""
    failures: list[str] = []
    active_spec_statuses = {"draft", "ready", "in-progress"}
    active_specs = []
    for spec_root in (ROOT / "specs", ROOT / "workspace" / "projects"):
        for path in sorted(spec_root.rglob("*SPEC.md")):
            if "/archive/" in path.as_posix():
                continue
            header = path.read_text(encoding="utf-8").split("---", 2)
            if len(header) < 3:
                continue
            status = next(
                (line.split(":", 1)[1].strip().strip('"\'') for line in header[1].splitlines()
                 if line.startswith("status:")),
                "",
            )
            if status in active_spec_statuses:
                active_specs.append(path)
    guidance = [
        *sorted((ROOT / ".assistant").rglob("*.md")),
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "src" / "odibi_anchor").rglob("*.md")),
        *active_specs,
    ]
    guidance = [path for path in guidance if ".assistant/references/snapshots/" not in path.as_posix()]
    for path in guidance:
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            context = " ".join(lines[max(0, index - 3):index + 4])
            lowered = line.lower()
            if any(phrase in lowered for phrase in BANNED_ALTERNATIVES):
                failures.append(f"{path.relative_to(ROOT)}:{index + 1}:semantic-alternative")
            if _legacy_call(line) and not _valid_compatibility_context(context):
                failures.append(f"{path.relative_to(ROOT)}:{index + 1}")
            if (
                (_legacy_learning_route(line) or _operational_learn_context_label(line))
                and not _valid_compatibility_context(context)
            ):
                failures.append(f"{path.relative_to(ROOT)}:{index + 1}:legacy-learning-route")
            if _confirm_call(line) and not _truthful_blocked_confirmation_context(context):
                failures.append(f"{path.relative_to(ROOT)}:{index + 1}:operational-confirm")
            if (
                any(phrase in lowered for phrase in OPERATIONAL_CONFIRMATION_PHRASES)
                and not _truthful_blocked_confirmation_context(context)
            ):
                failures.append(f"{path.relative_to(ROOT)}:{index + 1}:semantic-confirm")
    for path in sorted((ROOT / "src" / "odibi_anchor").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            docstring = ast.get_docstring(node, clean=False)
            if (
                docstring
                and (_legacy_learning_route(docstring) or _operational_learn_context_label(docstring))
                and not _valid_compatibility_context(docstring)
            ):
                failures.append(
                    f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 1)}:legacy-learning-docstring"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            lowered = value.lower()
            if any(phrase in lowered for phrase in BANNED_ALTERNATIVES):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:semantic-alternative")
            imperative = any(word in lowered for word in ("must", "run ", "use ", "blocking", "route"))
            if _legacy_call(value) and imperative and not _valid_compatibility_context(value):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:legacy-call")
            if _confirm_call(value) and not _truthful_blocked_confirmation_context(value):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:operational-confirm")
            if (
                any(phrase in lowered for phrase in OPERATIONAL_CONFIRMATION_PHRASES)
                and not _truthful_blocked_confirmation_context(value)
            ):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:semantic-confirm")
    assert failures == []


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
