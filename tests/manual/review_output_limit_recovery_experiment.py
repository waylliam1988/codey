"""Known read-only output truncation, one shared format repair, no unknown-send retry."""
import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app import review_service
from codey.providers.error_classification import OutputLengthError
from tests.manual.candidate_validation_and_behavioral_repair_experiment import ReportPlugin
from tests.manual.candidate_validation_and_behavioral_repair_experiment import variant as variant

APPROVED = '{"verdict":"approved","summary":"Current code meets the requirement","findings":[]}'


def request(reviewer, tmp_path):
    (tmp_path / 'app.py').write_text('answer = 42\n')
    reviewer.new_chat = Mock()
    reviewer.close = Mock()
    try:
        _, result = review_service.run_review_attempt(SimpleNamespace(emit=Mock(), state_home=tmp_path / 'state'),
            session_id='s', project=str(tmp_path), task='Set answer to 42.', writer_summary='Updated answer.',
            changes={'ok': True, 'files': [{'path': 'app.py', 'status': 'M'}], 'changed_count': 1, 'diff': ''},
            recent_log='', change_brief='', project_map='', verification_map='', review_impact_map='',
            execution_evidence='', reviewer_id='local', reviewer=reviewer, self_review=True, run_id='r')
        return result
    except review_service.ReviewSendUnknown:
        return None


def test_completed_but_truncated_review_can_obtain_one_new_complete_format_response(tmp_path):
    reviewer = SimpleNamespace(send=Mock(side_effect=[OutputLengthError('output truncated'), APPROVED]))
    result = request(reviewer, tmp_path)
    assert result is not None and result.approved
    assert reviewer.send.call_count == 2
    assert result.identity.snapshot_inventory_digest


@pytest.mark.parametrize('error', [RuntimeError('unknown transport result'), TimeoutError('unknown timeout')])
def test_unknown_request_outcome_never_resends_even_for_review(error, tmp_path):
    reviewer = SimpleNamespace(send=Mock(side_effect=[error, APPROVED]))
    assert request(reviewer, tmp_path) is None
    assert reviewer.send.call_count == 1


@pytest.mark.parametrize('second', [OutputLengthError('still truncated'), 'invalid JSON'])
def test_truncation_and_format_repair_share_one_retry_budget(second, tmp_path):
    replies = [OutputLengthError('output truncated'), second, APPROVED]
    reviewer = SimpleNamespace(send=Mock(side_effect=replies))
    try:
        result = request(reviewer, tmp_path)
    except ValueError:
        result = None
    assert result is None or not result.approved
    assert reviewer.send.call_count <= 2


def test_files_changed_during_truncated_review_cannot_obtain_fresh_approval(tmp_path):
    calls = 0

    def send(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            (tmp_path / 'app.py').write_text('answer = 99\n')
            raise OutputLengthError('output truncated')
        return APPROVED

    result = request(SimpleNamespace(send=Mock(side_effect=send)), tmp_path)
    assert result is not None and result.status == 'stale' and not result.approved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('current', 'candidate'), default='current')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    return pytest.main([str(Path(__file__).resolve()), '-q', '--tb=short',
                        '--experiment-variant=' + args.variant], plugins=[ReportPlugin(args.report, args.variant)])


if __name__ == '__main__':
    raise SystemExit(main())
