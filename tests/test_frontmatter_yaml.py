"""Each SKILL.md frontmatter must parse as strict YAML, as agent skill loaders read it."""

import yaml
from conftest import SKILLS_ROOT


def test_frontmatter_is_valid_yaml():
    broken = []
    for skill_md in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        try:
            frontmatter = yaml.safe_load(skill_md.read_text().split("---")[1])
        except yaml.YAMLError as e:
            broken.append(f"{skill_md.parent.name}: {str(e).splitlines()[0]}")
            continue
        if frontmatter.get("name") != skill_md.parent.name:
            broken.append(f"{skill_md.parent.name}: name is {frontmatter.get('name')!r}")
    assert not broken, f"invalid SKILL.md frontmatter (quote values holding ': '): {broken}"
