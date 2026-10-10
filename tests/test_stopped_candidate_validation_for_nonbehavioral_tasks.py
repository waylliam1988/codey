"""Recovery depends on admitted verification, not a punctuation task recognizer."""
import hashlib

import pytest

from tests.manual.candidate_validation_and_behavioral_repair_experiment import COMMAND, scenario

TASK = 'Fix names.py so invoice_total multiplies price and quantity. Do not modify tests. Run ' + COMMAND + '.'
INITIAL = 'def invoice_total(price, quantity): return price + quantity\n'
CORRECT = 'def invoice_total(price, quantity): return price * quantity\n'
WRONG = 'def invoice_total(price, quantity): return price * quantity + 1\n'
VISIBLE = ('import unittest\nfrom names import invoice_total\nclass Invoice(unittest.TestCase):\n'
           '    def test_total(self): self.assertEqual(invoice_total(7, 2), 14)\n')


@pytest.mark.parametrize('source,completed', [(CORRECT, True), (WRONG, False)])
def test_stopped_invoice_candidate_uses_actual_checks_and_review_without_a_behavioral_plan(tmp_path, source, completed):
    result = scenario(tmp_path, source=source, fault='runtime_validation',
                      task=TASK, initial=INITIAL, visible=VISIBLE)
    terminal = result['terminal']
    assert not [row for row in result['ledger'] if row['type'] == 'behavioral_plan_admitted']
    assert not [row for row in result['ledger'] if row['type'] == 'behavioral_observed']
    assert (terminal['stop_reason'] == 'done') is completed
    assert (result['project'] / 'test_names.py').read_text() == VISIBLE
    if completed:
        assert terminal['receipt']['verification']['checks_passed'] is True
        checks = [row for row in result['events'] if row['tool'] == 'run']
        assert checks[0]['exit_code'] == 1 and checks[-1]['exit_code'] == 0
        assert checks[-1]['source_sha256'] == hashlib.sha256((result['project'] / 'names.py').read_bytes()).hexdigest()
        assert result['reviews'][-1]['source'] == CORRECT
        assert result['proofs'][-1]['satisfied'] is True
