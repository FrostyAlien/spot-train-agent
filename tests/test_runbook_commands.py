"""Release check of the commands written in the skills (for maintainers; the installer does not copy /tests).

(a) Every `sky` subcommand and flag the agent runs is listed in the launch runbook's `flags()` start check, so
    the agent's own check covers it: the runbooks' code (blocks and inline spans) and the code blocks of the other
    skill files (`references/commands.md` diagnostics excepted). With SKY_BIN set (a `sky` binary of a supported
    version), every `sky` command in every skill file is also checked against that binary's `--help`, with the
    regex `flags()` uses.
(b) Every subcommand and `--flag` given to a bundled helper exists in that helper's source.
(c) Each paid or deploying action is written in one file only.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / 'skills'
GROUPS = {'jobs', 'api', 'storage', 'gpus', 'serve', 'volumes'}
SEPARATORS = re.compile(r'\|\||&&|<\(|\$\(|[;|()`]')
ALIASES = {'$R': 'render_job.py', '$G': 'campaign_guard.py', '$ST': 'stage.sh'}
HELPERS = {p.name: p for p in SKILLS.glob('*/scripts/*') if p.suffix in ('.py', '.sh') and not p.name.startswith('test_')}
PAID = ['sky jobs launch', 'sky api start', 'docker build', 'docker push']


def code_lines(path, inline=True):
    """Code lines of a markdown file: fenced block lines (continuations joined) and, with inline, code spans."""
    text = path.read_text()
    lines = []
    for block in re.findall(r'```[a-z]*\n(.*?)```', text, re.S):
        lines += re.sub(r'\\\n\s*', ' ', block).splitlines()
    if inline:
        lines += re.findall(r'`([^`\n]+)`', re.sub(r'```.*?```', '', text, flags=re.S))
    return [ln for ln in lines if '--help' not in ln]


def sky_commands(line):
    """(subcommand, [flags]) for every `sky` invocation in a shell line."""
    out = []
    for seg in SEPARATORS.split(line):
        tok = seg.split()
        for i, t in enumerate(tok):
            if (t == 'sky' or t.endswith('/bin/sky')) and i + 1 < len(tok) and re.fullmatch(r'[a-z][a-z-]*', tok[i + 1]):
                sub, rest = tok[i + 1], tok[i + 2:]
                if sub in GROUPS and rest and re.fullmatch(r'[a-z][a-z-]*', rest[0]):
                    sub, rest = f'{sub} {rest[0]}', rest[1:]
                flags = []
                for r in rest:
                    if re.match(r'\d*[<>]', r):
                        break
                    if re.fullmatch(r'--?[A-Za-z][\w-]*(=.*)?', r):
                        flags.append(r.split('=')[0])
                out.append((sub, flags))
                break
    return out


def skill_files(diagnostics=True):
    files = sorted(SKILLS.glob('*/SKILL.md')) + sorted(SKILLS.glob('*/references/*.md'))
    return [f for f in files if diagnostics or f.name != 'commands.md']


def start_check():
    """{subcommand: set(flags)} from the launch runbook's flags() block."""
    text = (SKILLS / 'gpu-training-launch/references/runbook.md').read_text()
    block = next(b for b in re.findall(r'```bash\n(.*?)```', text, re.S) if 'flags() {' in b)
    return {sub.strip('"'): set(flags.split())
            for sub, flags in re.findall(r'(?:^|;)\s*flags\s+("[^"]+"|[a-z-]+)([^;\n]*)', block, re.M)}


def name(f):
    return str(f.relative_to(SKILLS))


class RunbookCommands(unittest.TestCase):
    maxDiff = None

    def test_sky_flags_are_in_the_start_check(self):
        listed, missing = start_check(), []
        for f in skill_files(diagnostics=False):
            for line in code_lines(f, inline=f.name == 'runbook.md'):
                for sub, flags in sky_commands(line):
                    if sub not in listed:
                        missing.append(f'{name(f)}: sky {sub}')
                    missing += [f'{name(f)}: sky {sub} {x}' for x in flags if x not in listed.get(sub, flags)]
        self.assertEqual(sorted(set(missing)), [], "add these to the launch runbook's flags() block")

    @unittest.skipUnless(os.environ.get('SKY_BIN'), 'set SKY_BIN to check against a sky binary')
    def test_sky_flags_exist_in_sky_help(self):
        helps, missing = {}, []
        for f in skill_files():
            for line in code_lines(f):
                for sub, flags in sky_commands(line):
                    if sub not in helps:
                        r = subprocess.run([os.environ['SKY_BIN'], *sub.split(), '--help'], capture_output=True, text=True)
                        helps[sub] = r.stdout + r.stderr if r.returncode == 0 else None
                    if helps[sub] is None:
                        missing.append(f'{name(f)}: sky {sub}')
                    missing += [f'{name(f)}: sky {sub} {x}' for x in flags if helps[sub] is not None
                                and not re.search(rf'(^|[ ,]){re.escape(x)}([ ,=]|$)', helps[sub], re.M)]
        self.assertEqual(sorted(set(missing)), [])

    def test_helper_flags_exist(self):
        bad = []
        for f in skill_files():
            for line in code_lines(f):
                for seg in SEPARATORS.split(line):
                    tok = seg.split()
                    for i, t in enumerate(tok):
                        helper = ALIASES.get(t, Path(t).name)
                        if helper not in HELPERS:
                            continue
                        src = HELPERS[helper].read_text()
                        known = src + ''.join(p.read_text() for p in HELPERS[helper].parent.glob('*.py'))
                        args = tok[i + 1:tok.index('--', i) if '--' in tok[i:] else None]
                        if args and re.fullmatch(r'[a-z][a-z-]*', args[0]) and (
                                'add_subparsers' in src or helper in ('stage.sh', 'progress_watch.py')):
                            if not any(q in src for q in (f"'{args[0]}'", f'"{args[0]}"', f'  {args[0]})')):
                                bad.append(f'{name(f)}: {helper} {args[0]}')
                        for a in args:                 # a flag may be built in a loop: '--' + name, or a field name
                            flag = a.split('=')[0]
                            if re.fullmatch(r'--[a-z][\w-]*', flag) and not any(q in known for q in (
                                    f"'{flag}'", f'"{flag}"', f"'{flag[2:]}'", f"'{flag[2:].replace('-', '_')}'")):
                                bad.append(f'{name(f)}: {helper} {flag}')
                        break
        self.assertEqual(sorted(set(bad)), [])

    def test_each_paid_action_in_one_file(self):
        for action in PAID:
            files = {name(f) for f in skill_files() for line in code_lines(f, inline=False)
                     if re.search(rf'(^|[\s;&|(/]){re.escape(action)}\b', line)}
            self.assertEqual(len(files), 1, f'{action} is written in {sorted(files)}')


if __name__ == '__main__':
    unittest.main()
