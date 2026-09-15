"""Each skill directory must expose exactly one scripts/*.py entry point."""

from conftest import SKILLS_ROOT


def test_each_skill_has_exactly_one_script():
    extras = []
    missing = []
    for skill_dir in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
        if not (skill_dir / "SKILL.md").is_file():
            continue
        scripts = sorted((skill_dir / "scripts").glob("*.py"))
        if not scripts:
            missing.append(skill_dir.name)
        elif len(scripts) != 1:
            extras.append(f"{skill_dir.name}: {[p.name for p in scripts]}")
    assert not missing, f"skills with no scripts/*.py: {missing}"
    assert not extras, (
        "skills must have exactly one scripts/*.py "
        f"(helpers go in weather-skills-core): {extras}"
    )
