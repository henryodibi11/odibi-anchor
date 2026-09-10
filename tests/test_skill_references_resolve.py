"""H-004: every skills/<name>/SKILL.md referenced by a skill must exist.

Regression guard for the whole class of dangling-skill-reference bug
(verify-every-edit was the original instance).
"""
import os
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = _REPO / ".assistant" / "skills"
_REF = re.compile(r"skills/([a-z0-9\-]+)/SKILL\.md")


def test_all_referenced_skills_exist():
    available = {
        d for d in os.listdir(SKILLS_DIR)
        if (SKILLS_DIR / d / "SKILL.md").is_file()
    }
    missing = {}
    for d in available:
        text = (SKILLS_DIR / d / "SKILL.md").read_text(encoding="utf-8")
        for ref in _REF.findall(text):
            if ref not in available:
                missing.setdefault(ref, []).append(d)
    assert not missing, f"Dangling skill references: {missing}"
