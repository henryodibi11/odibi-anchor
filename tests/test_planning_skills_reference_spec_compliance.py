"""Formal specification guidance owns compliance requirements directly."""

from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent / ".assistant" / "skills" / "writing-specs"


def test_writing_specs_references_compliance_requirements_without_old_planning_skills():
    text = "\n".join(path.read_text(encoding="utf-8") for path in SKILL_ROOT.rglob("*.md")).casefold()
    assert "compliance requirements" in text or "section 10" in text
    assert "planning-delivery" not in text
    assert "aliases.json" not in text
