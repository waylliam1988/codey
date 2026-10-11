"""Predeclared paired bugs/controls and a native Writer cohort; oracles stay outside projects."""
from dataclasses import dataclass

from tests.manual.agent_stability_cases import OBSERVER

COMMAND = 'python -m unittest discover -v'


@dataclass(frozen=True)
class QualityCase:
    name: str
    task: str
    files: dict[str, str]
    candidate: str | None
    oracle: str
    buggy: bool | None


def cases():
    pairs = (
        ('inclusive-range', 'Return the input numbers between low and high, including both endpoints, in input order.',
         'def select(values, low, high):\n    return [n for n in values if low <= n <= high]\n',
         'def select(values, low, high):\n    return [n for n in values if low <= n < high]\n',
         "self.assertEqual(select([2,3,4], 1,5), [2,3,4])",
         "assert m.select([8, 2, 5, 2], 2, 8) == [8, 2, 5, 2]\nassert m.select([3, 3], 3, 3) == [3, 3]"),
        ('stable-key-dedup', 'Keep the first record for each id, in first-seen order. Different payloads with the same id are duplicates. Do not mutate input records.',
         'def select(records):\n    seen = set()\n    result = []\n    for row in records:\n        if row["id"] not in seen:\n            seen.add(row["id"])\n            result.append(row)\n    return result\n',
         'def select(records):\n    result = []\n    for row in records:\n        if row not in result:\n            result.append(row)\n    return result\n',
         "self.assertEqual(select([{'id':1}, {'id':1}, {'id':2}]), [{'id':1}, {'id':2}])",
         "rows = [{'id':3,'v':'first'}, {'id':1,'v':'a'}, {'id':3,'v':'later'}]\nbefore = repr(rows)\nassert m.select(rows) == rows[:2]\nassert repr(rows) == before"),
        ('nested-copy', 'Return an independent copy of the dictionary, including nested lists and dictionaries. Each copy retains its own values when nested values in the other copy are updated. Inputs are JSON-compatible.',
         'from copy import deepcopy\ndef select(record):\n    return deepcopy(record)\n',
         'def select(record):\n    return record.copy()\n',
         "self.assertEqual(select({'items':[{'n':1}]}), {'items':[{'n':1}]})",
         "value = {'items':[{'n':7}]}\nother = m.select(value)\nother['items'][0]['n'] = 8\nassert value['items'][0]['n'] == 7\nvalue['items'].append({'n':9})\nassert len(other['items']) == 1"),
    )
    results = []
    for family, requirement, correct, wrong, visible, oracle in pairs:
        task = f'Implement select in app.py. {requirement} Edit only app.py, do not modify tests or consumer.py. Run {COMMAND}.'
        test = 'import unittest\nfrom app import select\nclass Visible(unittest.TestCase):\n    def test_happy_path(self):\n        ' + visible + '\n' + OBSERVER
        context = ('from app import select\n\ndef use(record):\n    copied = select(record)\n    copied["items"][0]["n"] = 99\n    return record\n'
                   if family == 'nested-copy' else 'from app import select\n')
        for suffix, source in (('bug', wrong), ('correct', correct)):
            results.append(QualityCase(f'{family}-{suffix}', task,
                {'app.py': 'def select(*args):\n    raise NotImplementedError\n',
                 'test_app.py': test, 'consumer.py': context}, source, oracle, suffix == 'bug'))
    results.append(QualityCase('native-large-integer-ceil',
        'Implement select(n, d) in app.py, returning the mathematical ceiling of n/d. '
        'n is any integer (including negative and integers larger than 2**53), d is a positive integer. '
        f'Use exact integer arithmetic. Edit only app.py; do not modify tests. Run {COMMAND}.',
        {'app.py': 'def select(n, d):\n    return n // d\n',
         'test_app.py': 'import unittest\nfrom app import select\nclass Visible(unittest.TestCase):\n'
                        '    def test_ceil(self): self.assertEqual(select(7,3), 3)\n' + OBSERVER}, None,
        'for n, d in [(7,3),(-7,3),(-9,3),(2**60+1,3),(2**60+1,1),(0,7)]:\n    assert m.select(n,d) == -(-n//d)', None))
    return tuple(results)
