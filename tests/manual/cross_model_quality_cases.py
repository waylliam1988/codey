"""Held-out miniature projects with explicit contracts and independent oracles.

Reference implementations validate the experiment, and are never sent to models.
These are bounded project fixtures, not a claim of large-repository coverage.
"""
from __future__ import annotations

from dataclasses import dataclass

from tests.manual.coding_pipeline_cases import BY_NAME, CodingCase


@dataclass(frozen=True)
class QualityFixture:
    case: CodingCase
    reference: dict[str, str]
    mutants: tuple[dict[str, str], ...] = ()


def existing(name, task, files, target, checks, reference, mutant):
    return QualityFixture(
        CodingCase(name, task + " Keep the existing tests and other APIs unchanged.",
                   {**files, "pytest.ini": "[pytest]\n"}, (target,), (target,), checks),
        {target: reference}, ({target: mutant},),
    )


def greenfield(name, module, task, checks, reference, test, mutant):
    test_name = f"test_{module}.py"
    return QualityFixture(
        CodingCase(name, task + f" Create only {module}.py and {test_name}. "
                   "Write unittest tests covering normal and edge cases and run discovery.",
                   {}, (f"{module}.py", test_name), (f"{module}.py", test_name), checks),
        {f"{module}.py": reference, test_name: test}, ({f"{module}.py": mutant},),
    )


LEDGER = existing(
    "atomic-transfer",
    "Implement transfer(store, source, destination, amount) in transfers.py using store.balances. "
    "amount must be an integer excluding bool (TypeError otherwise), and nonnegative (ValueError otherwise). "
    "Both accounts must exist (KeyError otherwise), and source must have enough balance (ValueError otherwise). "
    "Validate everything before changing either balance. Zero transfers are allowed. A transfer to the same "
    "account validates all rules but changes nothing. Preserve the total balance and return None.",
    {"accounts.py": "class Accounts:\n    def __init__(self, balances):\n        self.balances = dict(balances)\n",
     "transfers.py": "def transfer(store, source, destination, amount):\n    raise NotImplementedError\n",
     "statements.py": "def total(store):\n    return sum(store.balances.values())\n",
     "test_transfers.py": "from accounts import Accounts\nfrom transfers import transfer\n"
                          "def test_transfer():\n    s = Accounts({'a': 5, 'b': 1})\n"
                          "    transfer(s, 'a', 'b', 2)\n    assert s.balances == {'a': 3, 'b': 3}\n"},
    "transfers.py",
    (("normal_total", "from accounts import Accounts\nfrom transfers import transfer\nfrom statements import total\ns=Accounts({'a':8,'b':2}); assert transfer(s,'a','b',3) is None; assert s.balances=={'a':5,'b':5} and total(s)==10"),
     ("same_account", "from accounts import Accounts\nfrom transfers import transfer\ns=Accounts({'a':5}); transfer(s,'a','a',3); assert s.balances=={'a':5}"),
     ("zero", "from accounts import Accounts\nfrom transfers import transfer\ns=Accounts({'a':0,'b':1}); transfer(s,'a','b',0); assert s.balances=={'a':0,'b':1}"),
     ("missing_atomic", "from accounts import Accounts\nfrom transfers import transfer\ns=Accounts({'a':5})\ntry: transfer(s,'a','missing',2)\nexcept KeyError: pass\nelse: raise AssertionError('missing accepted')\nassert s.balances=={'a':5}"),
     ("insufficient_atomic", "from accounts import Accounts\nfrom transfers import transfer\ns=Accounts({'a':1,'b':2})\ntry: transfer(s,'a','b',2)\nexcept ValueError: pass\nelse: raise AssertionError('overdraft')\nassert s.balances=={'a':1,'b':2}"),
     ("negative", "from accounts import Accounts\nfrom transfers import transfer\ns=Accounts({'a':2,'b':3})\ntry: transfer(s,'a','b',-1)\nexcept ValueError: pass\nelse: raise AssertionError('negative')\nassert s.balances=={'a':2,'b':3}"),
     ("types", "from accounts import Accounts\nfrom transfers import transfer\nfor v in (True,1.5,'2',None):\n s=Accounts({'a':5,'b':0})\n try: transfer(s,'a','b',v)\n except TypeError: pass\n else: raise AssertionError(repr(v))\n assert s.balances=={'a':5,'b':0}")),
    "def transfer(store, source, destination, amount):\n"
    "    if not isinstance(amount, int) or isinstance(amount, bool):\n        raise TypeError('integer required')\n"
    "    if amount < 0:\n        raise ValueError('negative')\n"
    "    old_source = store.balances[source]\n    old_destination = store.balances[destination]\n"
    "    if old_source < amount:\n        raise ValueError('insufficient')\n"
    "    if source != destination:\n        store.balances[source] = old_source - amount\n"
    "        store.balances[destination] = old_destination + amount\n",
    "def transfer(store, source, destination, amount):\n    store.balances[source] -= amount\n"
    "    store.balances[destination] += amount\n",
)

OVERLAY = existing(
    "config-isolation",
    "Implement overlay(defaults, overrides) in config_core.py. Inputs are dictionaries with JSON-like values. "
    "Recursively merge where both values are dictionaries; otherwise the override replaces the value, "
    "including lists and None. Preserve default-only keys. The result must share no mutable dict/list "
    "with either input, including override-only values. Never mutate inputs; settings.load_settings must "
    "therefore leave settings.DEFAULTS unchanged across calls.",
    {"config_core.py": "def overlay(defaults, overrides):\n    return {**defaults, **overrides}\n",
     "settings.py": "from config_core import overlay\nDEFAULTS = {'db': {'host': 'localhost', 'ports': [10]}, 'debug': False}\n"
                    "def load_settings(overrides):\n    return overlay(DEFAULTS, overrides)\n",
     "consumer.py": "from settings import load_settings\ndef host(overrides):\n    return load_settings(overrides)['db']['host']\n",
     "test_config.py": "from settings import load_settings\ndef test_flag():\n    assert load_settings({'debug': True})['debug'] is True\n"},
    "config_core.py",
    (("recursive", "from config_core import overlay\nassert overlay({'a':{'x':1,'y':2}},{'a':{'x':3}})=={'a':{'x':3,'y':2}}"),
     ("replace", "from config_core import overlay\nassert overlay({'a':[1],'b':{'x':1}},{'a':[2],'b':None})=={'a':[2],'b':None}"),
     ("defaults_isolation", "from config_core import overlay\na={'x':{'y':[1]}}; r=overlay(a,{}); r['x']['y'].append(2); assert a=={'x':{'y':[1]}}"),
     ("overrides_isolation", "from config_core import overlay\na={'x':{'y':[1]}}; r=overlay({},a); r['x']['y'].append(2); assert a=={'x':{'y':[1]}}"),
     ("unchanged", "from config_core import overlay\na={'a':{'x':[1],'y':2}}; b={'a':{'x':[3]}}; overlay(a,b); assert a=={'a':{'x':[1],'y':2}} and b=={'a':{'x':[3]}}"),
     ("consumer", "from consumer import host\nassert host({'db':{'ports':[20]}})=='localhost'"),
     ("repeat", "from settings import load_settings, DEFAULTS\na=load_settings({}); a['db']['ports'].append(99); assert load_settings({})['db']['ports']==[10] and DEFAULTS['db']['ports']==[10]")),
    "from copy import deepcopy\ndef overlay(defaults, overrides):\n    result = deepcopy(defaults)\n"
    "    for key, value in overrides.items():\n        if isinstance(result.get(key), dict) and isinstance(value, dict):\n"
    "            result[key] = overlay(result[key], value)\n        else:\n            result[key] = deepcopy(value)\n    return result\n",
    "def overlay(defaults, overrides):\n    return {**defaults, **overrides}\n",
)

EVENTS = existing(
    "event-revisions",
    "Implement latest_events(events) in events.py. Each event is a dict with string id and integer rev "
    "plus arbitrary fields. Normalize id with strip().lower(); ignore blank ids. For each normalized id "
    "choose the greatest rev; ties choose the later event. Return events ordered by first nonblank "
    "appearance of their normalized ids, even when replaced. Returned dicts must be new shallow copies "
    "with normalized id; never modify the input list or its dictionaries.",
    {"events.py": "def latest_events(events):\n    return list(events)\n",
     "feed.py": "from events import latest_events\ndef ids(events):\n    return [event['id'] for event in latest_events(events)]\n",
     "test_events.py": "from feed import ids\ndef test_single():\n    assert ids([{'id':'a','rev':1}]) == ['a']\n"},
    "events.py",
    (("normalization", "from events import latest_events as f\nassert f([{'id':' A ','rev':1},{'id':'a','rev':2}])==[{'id':'a','rev':2}]"),
     ("greatest", "from events import latest_events as f\nassert f([{'id':'a','rev':4},{'id':'A','rev':2}])==[{'id':'a','rev':4}]"),
     ("tie", "from events import latest_events as f\nassert f([{'id':'a','rev':1,'v':1},{'id':'a','rev':1,'v':2}])[0]['v']==2"),
     ("order", "from feed import ids\nassert ids([{'id':'b','rev':1},{'id':'a','rev':1},{'id':'B','rev':3}])==['b','a']"),
     ("blanks", "from events import latest_events as f\nassert f([{'id':'  ','rev':100}])==[] and f([])==[]"),
     ("copy", "from events import latest_events as f\na=[{'id':' A ','rev':1}]; r=f(a); assert a==[{'id':' A ','rev':1}]; r[0]['rev']=9; assert a[0]['rev']==1")),
    "def latest_events(events):\n    selected = {}\n    for event in events:\n        key = event['id'].strip().lower()\n"
    "        if key and (key not in selected or event['rev'] >= selected[key]['rev']):\n"
    "            selected[key] = dict(event, id=key)\n    return list(selected.values())\n",
    "def latest_events(events):\n    selected = {}\n    for event in events:\n"
    "        selected[event['id'].strip().lower()] = event\n    return list(selected.values())\n",
)

RANGES = existing(
    "range-validation",
    "Implement parse_ranges(text) in ranges.py. Accept comma-separated positive ASCII decimal integers "
    "and inclusive start-end ranges. Whitespace around tokens and range endpoints is allowed. Return "
    "the sorted unique integers. Empty or whitespace-only input returns []. Raise ValueError for zero, "
    "negative or reversed ranges, empty comma fragments, extra hyphens, non-ASCII digits, signs and "
    "non-integer tokens. Preserve exports.render_ranges which calls this API.",
    {"ranges.py": "def parse_ranges(text):\n    return [int(part) for part in text.split(',')]\n",
     "exports.py": "from ranges import parse_ranges\ndef render_ranges(text):\n    return ':'.join(str(n) for n in parse_ranges(text))\n",
     "test_ranges.py": "from ranges import parse_ranges\ndef test_simple():\n    assert parse_ranges('1,2') == [1,2]\n"},
    "ranges.py",
    (("expand", "from ranges import parse_ranges as f\nassert f('3-5,1,4')==[1,3,4,5]"),
     ("spaces", "from ranges import parse_ranges as f\nassert f(' 2 - 3 , 1 ')==[1,2,3]"),
     ("dedup", "from ranges import parse_ranges as f\nassert f('2,2,1-2,1')==[1,2]"),
     ("empty", "from ranges import parse_ranges as f\nassert f('')==[] and f('  ')==[]"),
     ("invalid", "from ranges import parse_ranges as f\nfor value in ('0','-1','3-1','1,,2','1,','1-2-3','+2','1.5','abc','١','1-0'):\n try: f(value)\n except ValueError: pass\n else: raise AssertionError(value)"),
     ("caller", "from exports import render_ranges\nassert render_ranges('3,1-2')=='1:2:3'")),
    "import re\ndef parse_ranges(text):\n    if not text.strip():\n        return []\n    result = set()\n"
    "    for token in text.split(','):\n        token = token.strip()\n"
    "        if not re.fullmatch(r'[0-9]+(?:\\s*-\\s*[0-9]+)?', token):\n            raise ValueError(token)\n"
    "        ends = [int(part.strip()) for part in token.split('-')]\n        start, end = ends[0], ends[-1]\n"
    "        if start <= 0 or end < start:\n            raise ValueError(token)\n"
    "        result.update(range(start, end + 1))\n    return sorted(result)\n",
    "def parse_ranges(text):\n    return [int(part) for part in text.split(',')]\n",
)

LRU = greenfield(
    "zero-lru", "cache",
    "Create cache.py with LRUCache(capacity), a positive-integer capacity (ValueError for <=0). "
    "put(key,value) inserts or replaces and makes key most recently used. get(key,default=None) "
    "returns default for missing keys, otherwise returns the stored value and promotes the key. "
    "When over capacity evict the least recently used key. Updating an existing key must not evict "
    "another key. Support len(cache) and keys() returning keys least-to-most recent. Instances are independent.",
    (("evict", "from cache import LRUCache\nc=LRUCache(2); c.put('a',1); c.put('b',2); c.put('c',3); assert c.get('a','missing')=='missing' and c.keys()==['b','c']"),
     ("promote", "from cache import LRUCache\nc=LRUCache(2); c.put('a',1); c.put('b',2); assert c.get('a')==1; c.put('c',3); assert c.keys()==['a','c']"),
     ("replace", "from cache import LRUCache\nc=LRUCache(2); c.put('a',1); c.put('b',2); c.put('a',3); assert len(c)==2 and c.keys()==['b','a'] and c.get('a')==3"),
     ("falsey", "from cache import LRUCache\nc=LRUCache(2); c.put('a',0); c.put('b',None); assert c.get('a',9)==0 and c.get('b',9) is None and c.get('missing',9)==9"),
     ("capacity", "from cache import LRUCache\nfor v in (0,-1):\n try: LRUCache(v)\n except ValueError: pass\n else: raise AssertionError(v)"),
     ("independent", "from cache import LRUCache\na=LRUCache(1); b=LRUCache(1); a.put('a',1); assert b.keys()==[] and len(b)==0")),
    "from collections import OrderedDict\nclass LRUCache:\n    def __init__(self, capacity):\n"
    "        if capacity <= 0:\n            raise ValueError('capacity')\n        self.capacity = capacity\n        self.data = OrderedDict()\n"
    "    def put(self, key, value):\n        self.data[key] = value\n        self.data.move_to_end(key)\n"
    "        if len(self.data) > self.capacity:\n            self.data.popitem(last=False)\n"
    "    def get(self, key, default=None):\n        if key not in self.data:\n            return default\n"
    "        self.data.move_to_end(key)\n        return self.data[key]\n"
    "    def __len__(self):\n        return len(self.data)\n    def keys(self):\n        return list(self.data)\n",
    "import unittest\nfrom cache import LRUCache\nclass Tests(unittest.TestCase):\n"
    "    def test_capacity(self):\n        c=LRUCache(1); c.put('a',1); c.put('b',2); self.assertEqual(c.keys(),['b'])\n",
    "class LRUCache:\n    def __init__(self, capacity):\n        self.data = {}\n"
    "    def put(self,key,value):\n        self.data[key]=value\n"
    "    def get(self,key,default=None):\n        return self.data.get(key,default)\n"
    "    def __len__(self):\n        return len(self.data)\n    def keys(self):\n        return list(self.data)\n",
)

TOPO = greenfield(
    "zero-dependencies", "workflow",
    "Create workflow.py with order_steps(graph), where graph maps each string step to an iterable of "
    "its dependency names. Return a list containing all steps with dependencies first; at each choice "
    "select the lexicographically smallest currently ready step. Repeated dependencies count once. "
    "Raise ValueError for any unknown dependency or a cycle, including a self-cycle. Empty graph returns []. "
    "Do not mutate graph or its dependency collections.",
    (("chain", "from workflow import order_steps as f\nassert f({'c':['b'],'a':[],'b':['a']})==['a','b','c']"),
     ("dynamic_ready", "from workflow import order_steps as f\nassert f({'z':[],'b':[],'a':['b']})==['b','a','z']"),
     ("duplicates", "from workflow import order_steps as f\nassert f({'b':['a','a'],'a':[]})==['a','b']"),
     ("cycle", "from workflow import order_steps as f\nfor graph in ({'a':['b'],'b':['a']},{'a':['a']}):\n try: f(graph)\n except ValueError: pass\n else: raise AssertionError('cycle')"),
     ("unknown", "from workflow import order_steps as f\ntry: f({'a':['missing']})\nexcept ValueError: pass\nelse: raise AssertionError('unknown')"),
     ("empty", "from workflow import order_steps as f\nassert f({})==[] and f({'z':[],'a':[]})==['a','z']"),
     ("immutable", "from workflow import order_steps as f\ng={'b':['a'],'a':[]}; f(g); assert g=={'b':['a'],'a':[]}")),
    "def order_steps(graph):\n    pending = {key:set(value) for key,value in graph.items()}\n"
    "    if any(not deps <= pending.keys() for deps in pending.values()):\n        raise ValueError('unknown')\n"
    "    result = []\n    while pending:\n        ready = sorted(key for key,deps in pending.items() if not deps)\n"
    "        if not ready:\n            raise ValueError('cycle')\n        key = ready[0]\n        result.append(key)\n"
    "        del pending[key]\n        for deps in pending.values():\n            deps.discard(key)\n    return result\n",
    "import unittest\nfrom workflow import order_steps\nclass Tests(unittest.TestCase):\n"
    "    def test_chain(self):\n        self.assertEqual(order_steps({'b':['a'],'a':[]}), ['a','b'])\n",
    "def order_steps(graph):\n    return sorted(graph)\n",
)

EXPIRY = greenfield(
    "zero-expiry", "expiry",
    "Create expiry.py with ExpiringCache(clock), using only the supplied callable clock for time. "
    "put(key,value,ttl) sets expiration at clock()+ttl; ttl<=0 removes any prior value immediately. "
    "get(key,default=None) returns the value only while clock()<expiration, otherwise deletes it and "
    "returns default. Replacing a key resets its expiration. purge() deletes all currently expired "
    "entries and returns the number deleted. Instances are independent; support falsey stored values. "
    "Tests must use a fake clock without sleeping.",
    (("expiry_boundary", "from expiry import ExpiringCache\nt=[0]; c=ExpiringCache(lambda:t[0]); c.put('a',1,2); t[0]=1; assert c.get('a')==1; t[0]=2; assert c.get('a','gone')=='gone'"),
     ("replace", "from expiry import ExpiringCache\nt=[0]; c=ExpiringCache(lambda:t[0]); c.put('a',1,1); t[0]=0.5; c.put('a',2,2); t[0]=1; assert c.get('a')==2; t[0]=2.5; assert c.get('a') is None"),
     ("nonpositive", "from expiry import ExpiringCache\nc=ExpiringCache(lambda:0)\nfor ttl in (0,-1):\n c.put('a',1,100); c.put('a',2,ttl); assert c.get('a','gone')=='gone'"),
     ("purge", "from expiry import ExpiringCache\nt=[0]; c=ExpiringCache(lambda:t[0]); c.put('a',1,1); c.put('b',2,1); c.put('c',3,2); t[0]=1; assert c.purge()==2 and c.purge()==0 and c.get('c')==3"),
     ("falsey", "from expiry import ExpiringCache\nc=ExpiringCache(lambda:0); c.put('a',False,2); c.put('b',None,2); assert c.get('a',9) is False and c.get('b',9) is None"),
     ("independent", "from expiry import ExpiringCache\na=ExpiringCache(lambda:0); b=ExpiringCache(lambda:0); a.put('x',1,1); assert b.get('x','gone')=='gone'")),
    "class ExpiringCache:\n    def __init__(self, clock):\n        self.clock = clock\n        self.data = {}\n"
    "    def put(self, key, value, ttl):\n        if ttl <= 0:\n            self.data.pop(key,None)\n"
    "        else:\n            self.data[key] = (value,self.clock()+ttl)\n"
    "    def get(self, key, default=None):\n        if key not in self.data:\n            return default\n"
    "        value,deadline = self.data[key]\n        if self.clock() >= deadline:\n            del self.data[key]\n            return default\n        return value\n"
    "    def purge(self):\n        now = self.clock()\n        expired = [key for key,(_,deadline) in self.data.items() if now >= deadline]\n"
    "        for key in expired:\n            del self.data[key]\n        return len(expired)\n",
    "import unittest\nfrom expiry import ExpiringCache\nclass Tests(unittest.TestCase):\n"
    "    def test_expiry(self):\n        t=[0]; c=ExpiringCache(lambda:t[0]); c.put('x',1,1); t[0]=1; self.assertIsNone(c.get('x'))\n",
    "class ExpiringCache:\n    def __init__(self, clock):\n        self.data = {}\n"
    "    def put(self,key,value,ttl):\n        self.data[key]=value\n"
    "    def get(self,key,default=None):\n        return self.data.get(key,default)\n"
    "    def purge(self):\n        return 0\n",
)

INTERVALS = greenfield(
    "zero-intervals", "intervals",
    "Create intervals.py with merge_intervals(intervals). Input is an iterable of integer (start,end) "
    "pairs denoting closed intervals. Return new tuples sorted by start with all overlapping intervals "
    "merged, including shared endpoints. Distinct adjacent integers without a shared endpoint do not "
    "overlap: (1,2),(3,4) stay separate. Raise ValueError if any start>end. Empty input returns []. "
    "Support negative endpoints, duplicates, single-point intervals and nested intervals. Never mutate input.",
    (("overlap", "from intervals import merge_intervals as f\nassert f([(3,5),(1,3),(8,9)])==[(1,5),(8,9)]"),
     ("adjacent", "from intervals import merge_intervals as f\nassert f([(1,2),(3,4)])==[(1,2),(3,4)]"),
     ("nested", "from intervals import merge_intervals as f\nassert f([(1,10),(2,3),(1,10),(10,10)])==[(1,10)]"),
     ("negative", "from intervals import merge_intervals as f\nassert f([(-3,-1),(-1,2),(7,7)])==[(-3,2),(7,7)]"),
     ("empty_generator", "from intervals import merge_intervals as f\nassert f([])==[] and f((v for v in [(3,4),(1,2)]))==[(1,2),(3,4)]"),
     ("invalid", "from intervals import merge_intervals as f\ntry: f([(1,2),(4,3)])\nexcept ValueError: pass\nelse: raise AssertionError('reversed')"),
     ("immutable", "from intervals import merge_intervals as f\na=[[3,5],[1,3]]; r=f(a); assert a==[[3,5],[1,3]] and all(isinstance(v,tuple) for v in r)")),
    "def merge_intervals(intervals):\n    values = [tuple(pair) for pair in intervals]\n"
    "    if any(start > end for start,end in values):\n        raise ValueError('reversed')\n"
    "    result = []\n    for start,end in sorted(values):\n        if result and start <= result[-1][1]:\n"
    "            result[-1] = (result[-1][0],max(result[-1][1],end))\n        else:\n            result.append((start,end))\n    return result\n",
    "import unittest\nfrom intervals import merge_intervals\nclass Tests(unittest.TestCase):\n"
    "    def test_overlap(self):\n        self.assertEqual(merge_intervals([(1,2),(2,3)]),[(1,3)])\n",
    "def merge_intervals(intervals):\n    return sorted(tuple(pair) for pair in intervals)\n",
)

FIXTURES = (
    QualityFixture(BY_NAME["cross-file-rename"], {
        "totals.py": "def total(values):\n    return sum(values)\n",
        "report.py": "from totals import total\ndef render(values):\n    return str(total(values))\n",
        "test_totals.py": "from totals import total\nfrom report import render\ndef test_total():\n    assert total([2,3])==5\ndef test_render():\n    assert render([2,3])=='5'\n",
    }), LEDGER, OVERLAY, EVENTS, RANGES,
    QualityFixture(BY_NAME["zero-slug"], {
        "slug.py": "import re\ndef slugify(text):\n    return re.sub('[^a-z0-9]+','-',text.strip().lower()).strip('-')\n",
        "test_slug.py": "import unittest\nfrom slug import slugify\nclass Tests(unittest.TestCase):\n    def test_slug(self):\n        self.assertEqual(slugify('A__B'),'a-b')\n",
    }), LRU, TOPO, EXPIRY, INTERVALS,
)
FIXTURE_BY_NAME = {fixture.case.name: fixture for fixture in FIXTURES}
