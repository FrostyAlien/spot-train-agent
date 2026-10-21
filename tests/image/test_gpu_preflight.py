import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-image'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import unittest

import tempfile

import gpu_preflight as gp
from gpu_preflight import coverage, evaluate, main, parse_arch, parse_arch_list, parse_nvidia_smi


class CoverageTests(unittest.TestCase):
    def test_parse_forms(self):
        self.assertEqual(parse_arch('sm_89'), [(8, 9, 'sass', '')])
        self.assertEqual(parse_arch('sm_120'), [(12, 0, 'sass', '')])
        self.assertEqual(parse_arch('sm_90a'), [(9, 0, 'sass', 'a')])
        self.assertEqual(parse_arch('compute_90'), [(9, 0, 'ptx', '')])
        self.assertEqual(parse_arch('8.9+PTX'), [(8, 9, 'sass', ''), (8, 9, 'ptx', '')])
        self.assertEqual(len(parse_arch_list('8.0;8.6 8.9,9.0+PTX')), 5)
        for bad in ('sm89', 'hopper', '8'):
            with self.assertRaises(ValueError):
                parse_arch(bad)

    def test_same_major_newer_minor_is_native(self):
        self.assertEqual(coverage('sm_80', (8, 9)), {'status': 'native', 'via': 'sm_80'})

    def test_older_minor_or_other_major_is_not_native(self):
        self.assertEqual(coverage('sm_86', (8, 0))['status'], 'unsupported')
        self.assertEqual(coverage('sm_80 sm_90', (10, 0))['status'], 'unsupported')

    def test_ptx_covers_newer_gpus_via_jit(self):
        self.assertEqual(coverage('sm_80 compute_90', (12, 0)), {'status': 'ptx-jit', 'via': 'compute_90'})
        self.assertEqual(coverage('compute_90', (8, 9))['status'], 'unsupported')

    def test_arch_specific_is_exact_only(self):
        self.assertEqual(coverage('sm_90a', (9, 0))['status'], 'native')
        self.assertEqual(coverage('sm_100a', (10, 3))['status'], 'unsupported')

    def test_cli_exit_codes(self):
        self.assertEqual(main(['coverage', '--arch-list', 'sm_80', '--capability', '8.9']), 0)
        self.assertEqual(main(['coverage', '--arch-list', 'sm_90', '--capability', '8.9']), 1)
        self.assertEqual(main(['coverage', '--arch-list', 'sm90', '--capability', '8.9']), 2)


def report(**over):
    base = {'arch_list': ['sm_80', 'sm_86', 'sm_90'], 'nccl_ok': True, 'smoke': {}, 'machine': 'x86_64',
            'devices': [{'index': i, 'name': 'L40S', 'capability': [8, 9], 'kernel_ok': True, 'ecc_uncorrected': 0}
                        for i in range(2)]}
    base.update(over)
    return base


class EvaluateTests(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(evaluate(report(), 2), {'ok': True, 'failures': [], 'warnings': []})

    def test_count_mismatch(self):
        self.assertFalse(evaluate(report(), 4)['ok'])

    def test_uncovered_architecture_fails(self):
        r = report(arch_list=['sm_90', 'sm_100'])
        self.assertIn('not covered', evaluate(r, 2)['failures'][0])

    def test_ptx_is_warning_unless_native_required(self):
        r = report(arch_list=['compute_80'])
        self.assertTrue(evaluate(r, 2)['ok'])
        self.assertEqual(len(evaluate(r, 2)['warnings']), 2)
        self.assertFalse(evaluate(report(arch_list=['compute_80']), 2, require_native=True)['ok'])

    def test_cuda_init_error_ecc_kernel_nccl_smoke(self):
        self.assertFalse(evaluate({'cuda_error': 'driver too old', 'devices': []}, 2)['ok'])
        r = report(); r['devices'][1]['ecc_uncorrected'] = 3
        self.assertIn('ECC', ' '.join(evaluate(r, 2)['failures']))
        r = report(); r['devices'][0].update(kernel_ok=False, kernel_error='no kernel image')
        self.assertIn('kernel', ' '.join(evaluate(r, 2)['failures']))
        self.assertFalse(evaluate(report(nccl_ok=False, nccl_error='timeout'), 2)['ok'])
        self.assertFalse(evaluate(report(nccl_ok=None), 2)['ok'])
        self.assertFalse(evaluate(report(smoke={'flash_attn': 'ImportError'}), 2)['ok'])

    def test_platform_must_match_host_cpu(self):
        self.assertTrue(evaluate(report(), 2, expected_platform='linux/amd64')['ok'])
        grace = report(machine='aarch64')
        self.assertTrue(evaluate(grace, 2, expected_platform='linux/arm64')['ok'])
        self.assertIn('does not match', evaluate(grace, 2, expected_platform='linux/amd64')['failures'][0])
        with self.assertRaises(ValueError):
            evaluate(report(), 2, expected_platform='linux/ppc64le')

    def test_single_gpu_needs_no_nccl(self):
        r = report(nccl_ok=None); r['devices'] = r['devices'][:1]
        self.assertTrue(evaluate(r, 1)['ok'])


class FrameworkNeutralTests(unittest.TestCase):
    SMI = ("0, NVIDIA L40S, 8.9, 580.65.06, 0\n"
           "1, NVIDIA L40S, 8.9, 580.65.06, 2\n")

    def test_parse_nvidia_smi(self):
        rows = parse_nvidia_smi(self.SMI)
        self.assertEqual(rows[0]['capability'], [8, 9])
        self.assertEqual(rows[1]['ecc_uncorrected'], 2)
        self.assertIsNone(parse_nvidia_smi("0, Old GPU, [N/A], 470.1, 0")[0]['capability'])

    def probe_none(self, smi=SMI, **kw):
        original = gp._nvidia_smi
        gp._nvidia_smi = lambda: parse_nvidia_smi(smi) if isinstance(smi, str) else smi
        try:
            return gp.probe(2, framework='none', **kw)
        finally:
            gp._nvidia_smi = original

    def test_any_framework_without_torch(self):
        r = self.probe_none(arch_list='8.0;8.6 8.9', smoke=['json:dumps'])
        self.assertEqual(r['framework'], 'none')
        result = evaluate(r, 2)
        self.assertIn('ECC', ' '.join(result['failures']))            # GPU 1 has ECC errors
        self.assertNotIn('not covered', ' '.join(result['failures']))

    def test_unchecked_items_are_warnings_not_silent_passes(self):
        result = evaluate(self.probe_none(smi="0, G, 8.9, 580, 0\n1, G, 8.9, 580, 0"), 2)
        self.assertTrue(result['ok'])
        self.assertTrue(any('coverage not checked' in w for w in result['warnings']))
        self.assertTrue(any('no kernel or collective test' in w for w in result['warnings']))

    def test_uncovered_and_unknown_capability_fail(self):
        result = evaluate(self.probe_none(smi="0, G, 8.9, 580, 0\n1, G, [N/A], 580, 0", arch_list='sm_90'), 2)
        text = ' '.join(result['failures'])
        self.assertIn('not covered', text)
        self.assertIn('capability unknown', text)

    def test_disk_budget(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ok = gp.disk_report([f'{tmp}=0.001'])
            self.assertTrue(gp.evaluate({'devices': [], 'disk': ok}, 0)['ok'])
            short = gp.disk_report([f'{tmp}=1000000'])
            failures = gp.evaluate({'devices': [], 'disk': short}, 0)['failures']
            self.assertIn('needs 1000000.0 GB', failures[0])
            missing = gp.disk_report(['/nonexistent/path=1'])
            self.assertIn('unknown', gp.evaluate({'devices': [], 'disk': missing}, 0)['failures'][0])
        with self.assertRaises(ValueError):
            gp.disk_report(['no-equals-sign'])

    def test_nvidia_smi_failure_fails(self):
        self.assertFalse(evaluate(self.probe_none(smi={'error': 'not found'}), 2)['ok'])


if __name__ == '__main__':
    unittest.main()
