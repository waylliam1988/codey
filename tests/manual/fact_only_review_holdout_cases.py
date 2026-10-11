"""Fresh task families; all reference behavior is scored only offline."""
import inspect

from tests.manual.agent_stability_cases import OBSERVER
from tests.manual.counterexample_review_cases import EvidenceCase
from tests.manual.named_probe_review_cases import same_json


def cases():
    families=(
        ('first-record-per-id',
         'Return a new list retaining only the first record for each id, in the order those ids first appear. '
         'records is a list of dictionaries, each with an integer id and a string label. Preserve the input.',
         'def select(records):\n    seen=set()\n    result=[]\n    for record in records:\n'
         '        if record["id"] not in seen:\n            seen.add(record["id"])\n            result.append(record)\n    return result\n',
         'def select(records):\n    return list({record["id"]:record for record in records}.values())\n',
         'self.assertEqual(select([{"id":1,"label":"a"},{"id":2,"label":"b"}]), [{"id":1,"label":"a"},{"id":2,"label":"b"}])',
         'v=[{"id":1,"label":"first"},{"id":2,"label":"second"},{"id":1,"label":"last"}]\n'
         'import copy\nbefore=copy.deepcopy(v)\nassert m.select(v)==v[:2]\nassert v==before\nassert m.select([])==[]'),
        ('remove-one-suffix',
         'Remove exactly one trailing occurrence of suffix from text when text ends with suffix; '
         'otherwise return text unchanged. Preserve all other occurrences. text and suffix are strings; suffix is nonempty.',
         'def select(text,suffix):\n    return text.removesuffix(suffix)\n',
         'def select(text,suffix):\n    return text.replace(suffix,"")\n',
         'self.assertEqual(select("draft.txt",".txt"),"draft")',
         'assert m.select("catcat","cat")=="cat"\nassert m.select("catX","cat")=="catX"\n'
         'assert m.select("xabab","ab")=="xab"\nassert m.select("","cat")==""'),
    )
    result=[]
    for name,requirement,correct,wrong,visible,oracle in families:
        task=('Implement select in app.py. '+requirement+
              ' Edit only app.py; do not modify tests or consumer.py. Run python -m unittest discover -v.')
        files={'app.py':'def select(*args, **kwargs):\n    raise NotImplementedError\n',
               'test_app.py':'import unittest\nfrom app import select\nclass Visible(unittest.TestCase):\n'
                            '    def test_happy(self):\n        '+visible+'\n'+OBSERVER,
               'consumer.py':'from app import select\n'}
        for label,source in (('bug',wrong),('correct',correct)):
            result.append(EvidenceCase(name+'-'+label,'holdout-v3',task,files,source,oracle,correct,label=='bug'))
    return tuple(result)


def score_probe(case,probe,actual):
    ns={}
    exec(case.reference,ns)
    try:
        bound=inspect.signature(ns['select']).bind(*probe['args'],**probe['kwargs'])
        values=bound.arguments
    except (TypeError,KeyError):
        return {'valid_domain':False,'expected_correct':None,'distinguishes':False}
    if case.name.startswith('first-record'):
        records=values['records']
        valid=(isinstance(records,list) and all(isinstance(r,dict) and type(r.get('id')) is int
               and isinstance(r.get('label'),str) for r in records))
    else:
        valid=all(isinstance(v,str) for v in values.values()) and bool(values['suffix'])
    if not valid:
        return {'valid_domain':False,'expected_correct':None,'distinguishes':False}
    expected=ns['select'](*probe['args'],**probe['kwargs'])
    return {'valid_domain':True,'reference_value':expected,'expected_correct':same_json(expected,probe['expected']),
            'distinguishes':actual.get('status')=='returned' and not same_json(expected,actual.get('value'))}
