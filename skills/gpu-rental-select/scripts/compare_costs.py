#!/usr/bin/env python3
"""Offline arithmetic only. Read normalized quotes, never access cloud accounts."""
import json
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path

CATEGORIES = ('storage', 'ingress', 'egress', 'controller', 'other')
UNITS = {'GB', 'GiB', 'GB_hour', 'GiB_hour', 'node_hour', 'request', 'item'}


def number(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,18}(?:\.\d{1,12})?', value):
        raise ValueError(f'{label}: use a bounded nonnegative decimal string')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'{label}: invalid decimal') from exc
    if not result.is_finite() or result < 0 or len(result.as_tuple().digits) > 18:
        raise ValueError(f'{label}: must be finite, nonnegative, <=18 digits')
    return result


def items(value, label):
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError(f'{label}: expected a bounded list')
    return value


def currency(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Z]{3}', value):
        raise ValueError('currency: expected three uppercase letters')
    return value


def compare(document):
    if not isinstance(document, dict) or type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise ValueError('schema_version must be integer 1')
    comparison_currency = currency(document['currency'])
    offers = items(document['offers'], 'offers')
    if not offers:
        raise ValueError('offers must not be empty')
    seen, results = set(), []
    target = None
    with localcontext() as context:
        context.prec = 80
        for offer in offers:
            identifier = offer['id']
            if not isinstance(identifier, str) or not identifier or len(identifier) > 256 or identifier in seen:
                raise ValueError('offer IDs must be unique nonempty strings')
            seen.add(identifier)
            if currency(offer['currency']) != comparison_currency:
                raise ValueError('currency mismatch; normalize quotes explicitly first')
            count = offer['gpu_count']
            if type(count) is not int or not 1 <= count <= 8:
                raise ValueError('gpu_count must be integer 1–8')
            useful = number(offer['useful_units'], 'useful_units')
            if offer['useful_unit'] not in ('tokens', 'steps'):
                raise ValueError('useful_unit must be tokens or steps')
            work = (useful, offer['useful_unit'])
            if target is not None and work != target:
                raise ValueError('offers must target equal useful work and units')
            target = work
            totals = {'compute': Decimal(0)}
            for segment in items(offer['compute_segments'], 'compute_segments'):
                rate = number(segment['rate'], 'rate')
                hours = number(segment['hours'], 'hours')
                quantum = number(segment['billing_quantum_seconds'], 'billing_quantum_seconds')
                minimum = number(segment['minimum_seconds'], 'minimum_seconds')
                if quantum <= 0:
                    raise ValueError('billing quantum must be positive')
                unit = segment['rate_unit']
                if unit not in ('node_hour', 'gpu_hour'):
                    raise ValueError('compute rate_unit must be node_hour or gpu_hour')
                seconds = max(hours * 3600, minimum)
                billed = (seconds / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum
                totals['compute'] += rate * billed / 3600 * (count if unit == 'gpu_hour' else 1)
            for category in CATEGORIES:
                totals[category] = Decimal(0)
                for component in items(offer[category], category):
                    if currency(component['currency']) != comparison_currency:
                        raise ValueError('currency mismatch in component')
                    if component['unit'] not in UNITS or component['unit'] != component['rate_unit']:
                        raise ValueError('unsupported or mismatched quantity/rate units')
                    totals[category] += number(component['quantity'], 'quantity') * number(component['rate'], 'rate')
            total = sum(totals.values())
            results.append({'id': identifier, 'currency': comparison_currency,
                            'components': {key: format(value, 'f') for key, value in totals.items()},
                            'total': format(total, 'f'), 'useful_unit': offer['useful_unit'],
                            'cost_per_useful_unit': format(total/useful, 'f') if useful else None,
                            'cost_per_million_tokens': format(total/useful*1000000, 'f') if useful and offer['useful_unit']=='tokens' else None})
        results.sort(key=lambda row: (Decimal(row['total']), row['id']))
    return {'schema_version': 1, 'currency': comparison_currency,
            'ranking_basis': 'total arithmetic cost; suitability must be checked separately', 'offers': results}


def reject_constant(value):
    raise ValueError(f'nonfinite JSON constant: {value}')


def main():
    if sys.argv[1:] in (['-h'], ['--help']):
        print(__doc__.strip() + '\nusage: compare_costs.py quotes.json')
        return
    if len(sys.argv) != 2:
        raise ValueError('usage: compare_costs.py quotes.json')
    with Path(sys.argv[1]).open('rb') as handle:
        raw = handle.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('input exceeds 2 MiB')
    document = json.loads(raw, parse_constant=reject_constant)
    print(json.dumps(compare(document), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError, RecursionError) as exc:
        print(f'Invalid input: {exc}', file=sys.stderr)
        sys.exit(2)
