"""Release check (not installed): every skill folder can be copied as is by an agent or the plugin loader.

- Each folder under skills/ has a SKILL.md whose frontmatter name is the folder name.
- Skills ship no dev tests (they live in /tests) and no symlinks (a copy must not reach outside the folder).
"""
import re
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / 'skills'


class LayoutTests(unittest.TestCase):
    def test_each_skill_has_a_matching_skill_md(self):
        dirs = [d for d in sorted(SKILLS.iterdir()) if d.is_dir()]
        self.assertEqual(len(dirs), 9)
        for d in dirs:
            text = (d / 'SKILL.md').read_text()
            m = re.search(r'^name:\s*(\S+)\s*$', text.split('---')[1], re.M)
            self.assertEqual(m and m.group(1), d.name)

    def test_skills_ship_no_dev_tests(self):
        found = [str(p.relative_to(SKILLS)) for p in SKILLS.rglob('test_*.py')]
        found += [str(p.relative_to(SKILLS)) for p in SKILLS.glob('*/tests')]
        self.assertEqual(found, [])

    def test_no_symlinks(self):
        self.assertEqual([str(p.relative_to(SKILLS)) for p in SKILLS.rglob('*') if p.is_symlink()], [])


if __name__ == '__main__':
    unittest.main()
