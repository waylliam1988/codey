"""Joint comparisons require matching arms and keep cost tradeoffs visible."""
import pytest

from tools.context_compaction_benchmark.matrix import comparison_matrix


def report(cost=12):
    return {'after_source_sha256':'same', 'rows':[
        {'arm':'before','case':'tool-heavy','seed':41,'success':True,'total_tokens':10,'seconds':2},
        {'arm':'after','case':'tool-heavy','seed':41,'success':True,'total_tokens':cost,'seconds':1}]}


def test_joint_report_does_not_call_extra_cost_a_universal_win():
    result = comparison_matrix({'old-codey':report(), 'opencode':report(8), 'pi':report(9)})
    assert result['comparisons']['old-codey']['verdict'] == 'tradeoff'
    assert result['comparisons']['pi']['verdict'] == 'cost_gain'
    assert not result['dominates_all_measured_metrics']
    assert result['comparisons']['opencode']['cases']['tool-heavy']['after']['tokens'] == 8


def test_joint_report_rejects_mixed_codey_snapshots_or_unpaired_rows():
    first, second = report(), report()
    second['after_source_sha256'] = 'other'
    with pytest.raises(ValueError, match='snapshot'):
        comparison_matrix({'old-codey':first, 'pi':second})
    second = report()
    second['rows'].pop()
    with pytest.raises(ValueError, match='paired'):
        comparison_matrix({'pi':second})


def test_worker_crash_is_inconclusive_and_does_not_turn_missing_usage_into_zero():
    value = report()
    value['rows'][0] = {'arm':'before','case':'tool-heavy','seed':41,'success':False,'error':'worker failed'}
    result = comparison_matrix({'pi':value})
    assert result['comparisons']['pi']['verdict'] == 'inconclusive'
    assert result['comparisons']['pi']['cases']['tool-heavy']['before']['tokens'] is None
    assert not result['dominates_all_measured_metrics']


def test_history_only_replays_do_not_claim_to_have_measured_duplicate_execution():
    result = comparison_matrix({'pi':report(8)})
    assert result['comparisons']['pi']['cases']['tool-heavy']['after']['duplicate_executions'] is None
