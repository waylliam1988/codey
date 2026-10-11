"""Predeclared development/holdout tasks and offline-only reference functions."""
from dataclasses import dataclass

from tests.manual.agent_stability_cases import OBSERVER


@dataclass(frozen=True)
class EvidenceCase:
    name: str
    cohort: str
    task: str
    files: dict
    candidate: str
    oracle: str
    reference: str
    buggy: bool


def cases():
    families = (
        ('zero-bound', 'development',
         'Clamp value to the optional inclusive low and high bounds. None means absent. Zero is a valid bound. '
         'value and present bounds are integers; when both bounds exist, low <= high.',
         'def select(value, low=None, high=None):\n    if low is not None: value = max(value, low)\n'
         '    if high is not None: value = min(value, high)\n    return value\n',
         'def select(value, low=None, high=None):\n    if low: value = max(value, low)\n'
         '    if high: value = min(value, high)\n    return value\n',
         'self.assertEqual(select(7, 2, 10), 7)',
         'assert m.select(-5,low=0)==0\nassert m.select(5,high=0)==0\nassert m.select(5,low=0)==5'),
        ('ordered-range', 'development',
         'Return all integers from start through end inclusive. Inputs are integers satisfying start <= end.',
         'def select(start,end):\n    return list(range(start,end+1))\n',
         'def select(start,end):\n    return list(range(start,end)) if start == end else list(range(start,end+1))\n',
         'self.assertEqual(select(2,4), [2,3,4])',
         'assert m.select(3,3)==[3]\nassert m.select(-2,0)==[-2,-1,0]'),
        ('exact-truncation', 'holdout',
         'Return integer a divided by nonzero integer b, truncated toward zero. '
         'Inputs include arbitrarily large integers and either sign. Use exact integer arithmetic.',
         'def select(a,b):\n    q=abs(a)//abs(b)\n    return -q if (a<0)!=(b<0) else q\n',
         'def select(a,b):\n    return int(a/b)\n',
         'self.assertEqual(select(7,3), 2)',
         'for a,b in [(2**80+5,3),(-7,3),(7,-3),(-7,-3),(2**80+1,1)]:\n'
         '    q=abs(a)//abs(b)\n    assert m.select(a,b)==(-q if (a<0)!=(b<0) else q)'),
        ('missing-defaults', 'holdout',
         'Return a new dictionary containing settings plus defaults for keys absent from settings. '
         'Existing values including 0, False, empty strings and None take precedence. '
         'Inputs are dictionaries with string keys and JSON values. Preserve both input dictionaries.',
         'def select(settings,defaults):\n    return {**defaults,**settings}\n',
         'def select(settings,defaults):\n    result=dict(settings)\n    for key,value in defaults.items():\n'
         '        if not result.get(key): result[key]=value\n    return result\n',
         'self.assertEqual(select({"port":8080},{"port":80,"mode":"safe"}), {"port":8080,"mode":"safe"})',
         's={"x":0,"y":False,"z":None,"w":""}; d={"x":9,"y":True,"z":1,"w":"default","a":1}\n'
         'before=(repr(s),repr(d))\nassert m.select(s,d)=={**d,**s}\nassert (repr(s),repr(d))==before'),
    )
    result = []
    for name, cohort, requirement, correct, wrong, visible, oracle in families:
        task = ('Implement select in app.py. ' + requirement
                + ' Edit only app.py; do not modify tests or consumer.py. Run python -m unittest discover -v.')
        files = {'app.py': 'def select(*args, **kwargs):\n    raise NotImplementedError\n',
                 'test_app.py': 'import unittest\nfrom app import select\nclass Visible(unittest.TestCase):\n'
                 '    def test_happy(self):\n        ' + visible + '\n' + OBSERVER,
                 'consumer.py': 'from app import select\n'}
        for label, candidate in (('bug', wrong), ('correct', correct)):
            result.append(EvidenceCase(name + '-' + label, cohort, task, files, candidate, oracle, correct, label == 'bug'))
    return tuple(result)


def domain(case, probe):
    """Offline input-domain scoring; never imported by the reviewer/worker."""
    import inspect
    namespace = {}
    exec(case.reference, namespace)
    try:
        args = inspect.signature(namespace['select']).bind(*probe['args'], **probe['kwargs'])
        args.apply_defaults()
    except (TypeError, KeyError):
        return False
    values = args.arguments
    if case.name.startswith('zero-bound'):
        v, lo, hi = values['value'], values['low'], values['high']
        return type(v) is int and all(x is None or type(x) is int for x in (lo, hi)) and (
            lo is None or hi is None or lo <= hi)
    if case.name.startswith('ordered-range'):
        a, b = values['start'], values['end']
        return type(a) is int and type(b) is int and a <= b and b-a <= 1000
    if case.name.startswith('exact-truncation'):
        return all(type(v) is int for v in values.values()) and values['b'] != 0
    return all(isinstance(v, dict) and all(isinstance(k, str) for k in v) for v in values.values())


def score_probe(case, probe, actual):
    """Independent fixture reference evaluates validity AFTER the live arm finishes."""
    if not domain(case, probe):
        return {'valid_domain': False, 'expected_correct': None, 'distinguishes': False}
    namespace = {}
    exec(case.reference, namespace)
    expected = namespace['select'](*probe['args'], **probe['kwargs'])
    return {'valid_domain': True, 'expected_correct': probe['expected'] == expected,
            'reference_value': expected,
            'distinguishes': actual.get('status') == 'returned' and actual.get('value') != expected}
