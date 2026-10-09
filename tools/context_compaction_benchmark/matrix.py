"""Aggregate paired trials without confusing replay scope with product quality."""
from collections import defaultdict

from tools.context_compaction_benchmark.scorer import verdict


def arm_stats(rows):
    known = all(type(row.get('total_tokens')) is int for row in rows)
    timed = all(type(row.get('seconds')) in {int, float} for row in rows)
    executed = all(type(row.get('duplicate_executions')) is int for row in rows)
    return {'successes':sum(row['success'] for row in rows), 'trials':len(rows),
            'tokens':sum(row['total_tokens'] for row in rows) if known else None,
            'seconds':round(sum(row['seconds'] for row in rows), 3) if timed else None,
            'duplicate_executions':sum(row['duplicate_executions'] for row in rows) if executed else None,
            'infrastructure_errors':sum('error' in row for row in rows)}


def comparison_matrix(reports):
    if not reports or len({value['after_source_sha256'] for value in reports.values()}) != 1:
        raise ValueError('joint comparison requires the same frozen Codey snapshot')
    comparisons, dominates = {}, True
    for name, report in reports.items():
        arms = {arm:[row for row in report['rows'] if row['arm'] == arm and not row.get('robustness_only')]
                for arm in ('before', 'after')}
        keys = {arm:[(row['case'],row['seed']) for row in rows] for arm, rows in arms.items()}
        if not keys['before'] or set(keys['before']) != set(keys['after']) or any(len(set(value)) != len(value) for value in keys.values()):
            raise ValueError('joint comparison requires unique paired case/seed rows')
        cases = defaultdict(dict)
        for arm, rows in arms.items():
            for case in {row['case'] for row in rows}:
                cases[case][arm] = arm_stats([row for row in rows if row['case'] == case])
        result = verdict(arms['before'], arms['after'])
        dominates &= result not in {'regression', 'inconclusive'}
        comparisons[name] = {'verdict':result,'cases':dict(cases)}
        for values in cases.values():
            before, after = values['before'], values['after']
            dominates &= (not before['infrastructure_errors'] and not after['infrastructure_errors']
                          and after['successes'] >= before['successes'] and after['duplicate_executions'] == 0
                          and before['tokens'] is not None and after['tokens'] is not None
                          and before['seconds'] is not None and after['seconds'] is not None
                          and after['tokens'] <= before['tokens'] and after['seconds'] <= before['seconds'])
    return {'after_source_sha256':next(iter(reports.values()))['after_source_sha256'],
            'comparisons':comparisons,'dominates_all_measured_metrics':bool(dominates),
            'scope':'Local algorithm replay; no claim about complete reference agents or other models'}
