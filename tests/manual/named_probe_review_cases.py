"""New frozen semantic tasks; requirement oracles are strictly offline."""
import inspect
import json

from tests.manual.agent_stability_cases import OBSERVER
from tests.manual.counterexample_review_cases import EvidenceCase


def cases():
    families = (
        ('rightmost-match',
         'Return the index of the last occurrence of target in values, or -1 if absent. '
         'values is a list of integers and target is an integer. Preserve values.',
         'def select(values,target):\n    for i in range(len(values)-1,-1,-1):\n'
         '        if values[i]==target: return i\n    return -1\n',
         'def select(values,target):\n    try: return values.index(target)\n    except ValueError: return -1\n',
         'self.assertEqual(select([4,6,8],6),1)',
         'assert m.select([4,6,4,6],6)==3\nassert m.select([],1)==-1\n'
         'v=[1,2,1]; assert m.select(v,1)==2; assert v==[1,2,1]'),
        ('overlapping-occurrences',
         'Count all occurrences of needle in text, including overlapping occurrences. '
         'text and needle are strings; needle is nonempty. Matching is case-sensitive.',
         'def select(text,needle):\n    return sum(text.startswith(needle,i) for i in range(len(text)))\n',
         'def select(text,needle):\n    return text.count(needle)\n',
         'self.assertEqual(select("one-x-one","one"),2)',
         'assert m.select("aaaaa","aa")==4\nassert m.select("ababa","aba")==2\n'
         'assert m.select("","a")==0\nassert m.select("AaA","a")==1'),
    )
    result = []
    for name, requirement, correct, wrong, visible, oracle in families:
        task = ('Implement select in app.py. ' + requirement
                + ' Edit only app.py; do not modify tests or consumer.py. Run python -m unittest discover -v.')
        files = {'app.py': 'def select(*args, **kwargs):\n    raise NotImplementedError\n',
                 'test_app.py': 'import unittest\nfrom app import select\nclass Visible(unittest.TestCase):\n'
                 '    def test_happy(self):\n        ' + visible + '\n' + OBSERVER,
                 'consumer.py': 'from app import select\n'}
        for label, source in (('bug', wrong), ('correct', correct)):
            result.append(EvidenceCase(name+'-'+label, 'holdout-v2', task, files, source, oracle, correct, label=='bug'))
    return tuple(result)


def same_json(left, right):
    # JSON booleans, strings and numbers must not collapse through Python ==.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def score_probe(case, probe, actual):
    ns = {}
    exec(case.reference, ns)
    try:
        bound = inspect.signature(ns['select']).bind(*probe['args'], **probe['kwargs'])
        values = bound.arguments
    except (TypeError, KeyError):
        return {'valid_domain': False, 'expected_correct': None, 'distinguishes': False}
    if case.name.startswith('rightmost-match'):
        valid = (isinstance(values['values'], list) and all(type(x) is int for x in values['values'])
                 and type(values['target']) is int)
    else:
        valid = all(isinstance(v, str) for v in values.values()) and bool(values['needle'])
    if not valid:
        return {'valid_domain': False, 'expected_correct': None, 'distinguishes': False}
    expected = ns['select'](*probe['args'], **probe['kwargs'])
    return {'valid_domain': True, 'reference_value': expected, 'expected_correct': same_json(expected, probe['expected']),
            'distinguishes': actual.get('status') == 'returned' and not same_json(actual.get('value'), expected)}
