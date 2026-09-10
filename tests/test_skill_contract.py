"""Lint the native skill-file contract."""
import os

SKILLS_DIR = os.path.join(".assistant", "skills")

def _all_skills():
    return sorted(
        d for d in os.listdir(SKILLS_DIR)
        if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
    )


def test_new_skills_declare_when_not_to_load_and_enforcement():
    failures = []
    for name in _all_skills():
        with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), encoding="utf-8") as f:
            text = f.read().lower()
        if "when not to load" not in text:
            failures.append(f"{name}: missing 'When NOT to load'")
        if "## enforcement" not in text and "enforcement:" not in text:
            failures.append(f"{name}: missing 'Enforcement' label")
    assert not failures, "\n".join(failures)
