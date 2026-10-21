"""Release check (not installed): every pointer inside the skills resolves, so a fact that moved can still be found.

- Markdown links from a skill doc to a file stay inside the installed skills, exist, and their #anchor matches a
  heading of the target (headings inside fenced code do not count).
- Cross-skill pointers written as `gpu-xxx` → `references/name.md#anchor`, or with the short skill name
  ("lifecycle `references/name.md`"), resolve the same way.
- Every reference file is named in its skill's SKILL.md (the router) on a line that says when to read it.
Not checked: pointers by section or step number ("§4", "SKILL step 8").
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'skills'
LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')
POINTER = re.compile(r'`(gpu-[a-z-]+)`\s*(?:→|->)\s*`((?:references|scripts|assets)/[^`#\s]+)(#[^`\s]+)?`')
SHORT = re.compile(r'\b(lifecycle|select|image|data|controller|launch|recover|observe|finish)(?: skill)?(?:[\'’]s)? '
                   r'`((?:references|scripts|assets)/[^`#\s]+)(#[^`\s]+)?`')
FOLDER = {'select': 'gpu-rental-select'}


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    s = re.sub(r'[^\w\- ]', '', heading.strip().lower())
    return s.replace(' ', '-')


def anchors(path: Path) -> set:
    out, fenced = set(), False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        m = re.match(r'#{1,6}\s+(.*)', line)
        if m and not fenced:
            out.add(slug(m.group(1)))
    return out


def docs():
    return sorted(SKILLS.glob('*/SKILL.md')) + sorted(SKILLS.glob('*/references/*.md'))


class LinkTests(unittest.TestCase):
    def check(self, source: Path, target: Path, anchor: str, problems: list, label: str):
        if not target.exists():
            problems.append(f'{source.relative_to(ROOT)}: {label} -> missing file')
        elif anchor and target.suffix == '.md' and anchor not in anchors(target):
            problems.append(f'{source.relative_to(ROOT)}: {label} -> no heading #{anchor}')

    def test_markdown_links_and_anchors_resolve(self):
        problems = []
        for doc in docs():
            for href in LINK.findall(doc.read_text()):
                if re.match(r'[a-z]+:', href):                     # http(s), mailto: not checked here
                    continue
                path, _, anchor = href.partition('#')
                target = doc if not path else (doc.parent / path).resolve()
                if SKILLS.resolve() not in target.parents:
                    problems.append(f'{doc.relative_to(ROOT)}: {href} -> outside the installed skills')
                    continue
                self.check(doc, target, anchor, problems, href)
        self.assertEqual(problems, [])

    def test_cross_skill_pointers_resolve(self):
        problems, names = [], {p.parent.name for p in SKILLS.glob('*/SKILL.md')}
        for doc in docs():
            text = doc.read_text()
            found = POINTER.findall(text) + [(FOLDER.get(s, f'gpu-training-{s}'), rel, a) for s, rel, a in SHORT.findall(text)]
            for skill, rel, anchor in found:
                if skill not in names:
                    problems.append(f'{doc.relative_to(ROOT)}: unknown skill {skill}')
                    continue
                self.check(doc, SKILLS / skill / rel, (anchor or '#')[1:], problems, f'{skill} -> {rel}{anchor}')
        self.assertEqual(problems, [])

    def test_every_reference_has_a_read_when_line_in_its_router(self):
        missing = []
        for r in sorted(SKILLS.glob('*/references/*.md')):
            lines = [ln for ln in (r.parents[1] / 'SKILL.md').read_text().splitlines() if r.name in ln]
            if not any(re.search(r'\bread\b', ln, re.I) for ln in lines):
                missing.append(str(r.relative_to(ROOT)))
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
