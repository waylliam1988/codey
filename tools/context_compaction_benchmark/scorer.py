"""Exact continuation scoring; missing usage stays unknown."""
import json


def score_answer(text, expected):
    try:
        answer = json.loads(text.strip().removeprefix('```json').removesuffix('```').strip())
    except (ValueError, TypeError):
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    checks = {'constraints': answer.get('constraint') == expected['constraint'],
              'latest_correction': answer.get('target') == expected['target'],
              'evidence': type(answer.get('exit_code')) is int and answer['exit_code'] == expected['exit_code'],
              'result_reference': answer.get('result_ref') == expected['result_ref']}
    if 'observations' in expected:
        checks['observation_recovery'] = answer.get('observations') == expected['observations']
    return checks | {'success': all(checks.values())}


def score_trial(row, expected):
    scored = score_answer(row['answer'], expected)
    scored['success'] &= row.get('success') is not False and 'error' not in row and 'failure_kind' not in row
    return row | scored


def verdict(before, after):
    if not before or len(before) != len(after) or any('error' in row for row in before + after):
        return 'inconclusive'
    if any('case' not in row or 'seed' not in row for row in before + after):
        return 'inconclusive'
    pairs = {arm: {(row['case'], row['seed']): row for row in rows}
             for arm, rows in (('before', before), ('after', after))}
    if (pairs['before'].keys() != pairs['after'].keys()
            or len(pairs['before']) != len(before) or len(pairs['after']) != len(after)):
        return 'inconclusive'
    if any(pairs['before'][key]['success'] and not pairs['after'][key]['success'] for key in pairs['before']):
        return 'regression'
    a, b = sum(row['success'] for row in before), sum(row['success'] for row in after)
    if any(row.get('duplicate_executions', 0) for row in after) or b < a:
        return 'regression'
    if b > a:
        measured = all(type(row.get('total_tokens')) is int for row in before + after)
        more_cost = measured and sum(row['total_tokens'] for row in after) > sum(row['total_tokens'] for row in before)
        return 'quality_gain_with_cost' if more_cost else 'quality_gain'
    costs = [row.get('total_tokens') for row in before + after]
    if all(type(cost) is int for cost in costs):
        first, second = sum(row['total_tokens'] for row in before), sum(row['total_tokens'] for row in after)
        if second < first:
            return 'cost_gain'
        if second > first:
            return 'tradeoff'
    return 'no_gain'
