import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-rental-select'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import copy
import unittest
from compare_costs import compare


def fixture():
    return {'schema_version': 1, 'currency': 'USD', 'offers': [{
        'id': 'synthetic', 'currency': 'USD', 'gpu_count': 8,
        'useful_units': '1000000', 'useful_unit': 'tokens',
        'compute_segments': [{'label': 'training', 'rate': '2', 'rate_unit': 'gpu_hour',
                              'hours': '2', 'billing_quantum_seconds': '1', 'minimum_seconds': '0'}],
        **{key: [] for key in ('storage', 'ingress', 'egress', 'controller', 'other')}}]}


class CostTests(unittest.TestCase):
    def total(self, d):
        return compare(d)['offers'][0]

    def test_gpu_vs_node(self):
        d = fixture()
        self.assertEqual(self.total(d)['total'], '32')
        d['offers'][0]['compute_segments'][0]['rate_unit'] = 'node_hour'
        self.assertEqual(self.total(d)['total'], '4')

    def test_all_costs_and_replay(self):
        d = fixture(); o = d['offers'][0]
        replay = copy.deepcopy(o['compute_segments'][0]); replay['hours'] = '1'
        o['compute_segments'].append(replay)
        for category in ('storage', 'ingress', 'egress', 'controller', 'other'):
            o[category] = [{'label': category, 'quantity': '2', 'rate': '3', 'unit': 'item', 'rate_unit': 'item', 'currency': 'USD'}]
        self.assertEqual(self.total(d)['total'], '78')
        self.assertEqual(self.total(d)['cost_per_million_tokens'], '78.000000')

    def test_zero_work(self):
        d = fixture(); d['offers'][0]['useful_units'] = '0'
        self.assertIsNone(self.total(d)['cost_per_useful_unit'])

    def test_reject_currency(self):
        d = fixture(); d['offers'][0]['currency'] = 'EUR'
        with self.assertRaises(ValueError): compare(d)

    def test_reject_missing_unknown(self):
        d = fixture(); del d['offers'][0]['egress']
        with self.assertRaises(KeyError): compare(d)
        d = fixture(); d['offers'][0]['compute_segments'][0]['rate'] = None
        with self.assertRaises(ValueError): compare(d)

    def test_finite_nonnegative_and_bounded(self):
        for bad in ('NaN', 'Infinity', '-1', '1e999999', '9'*19, 1, True):
            d = fixture(); d['offers'][0]['compute_segments'][0]['rate'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): compare(d)

    def test_gpu_count(self):
        for bad in (0, 9, True, 1.5):
            d = fixture(); d['offers'][0]['gpu_count'] = bad
            with self.assertRaises(ValueError): compare(d)

    def test_units(self):
        d = fixture(); d['offers'][0]['storage'] = [{'label': 'disk', 'quantity': '1', 'rate': '1', 'unit': 'GB', 'rate_unit': 'GiB', 'currency': 'USD'}]
        with self.assertRaises(ValueError): compare(d)

    def test_billing_quantum_minimum(self):
        d = fixture(); s = d['offers'][0]['compute_segments'][0]
        s.update(rate='3600', rate_unit='node_hour', hours='0.001', billing_quantum_seconds='1', minimum_seconds='0')
        self.assertEqual(self.total(d)['total'], '4')
        s['minimum_seconds'] = '60'
        self.assertEqual(float(self.total(d)['total']), 60)

    def test_mismatched_work(self):
        d = fixture(); other = copy.deepcopy(d['offers'][0]); other['id'] = 'other'; other['useful_units'] = '1'; d['offers'].append(other)
        with self.assertRaises(ValueError): compare(d)

    def test_component_currency(self):
        d = fixture(); d['offers'][0]['other'] = [{'quantity': '1', 'rate': '1', 'unit': 'item', 'rate_unit': 'item', 'currency': 'EUR'}]
        with self.assertRaises(ValueError): compare(d)

    def test_duplicate_ids(self):
        d = fixture(); d['offers'].append(copy.deepcopy(d['offers'][0]))
        with self.assertRaises(ValueError): compare(d)

    def test_help(self):
        import subprocess
        r = subprocess.run([_sys.executable, str(SKILL / 'scripts' / 'compare_costs.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0); self.assertIn('usage: compare_costs.py quotes.json', r.stdout)


if __name__ == '__main__': unittest.main()
