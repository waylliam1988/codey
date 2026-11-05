"""Fixed coding tasks and external behavioral oracles for pipeline value A/B."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodingCase:
    name: str
    task: str
    files: dict[str, str]
    allowed: tuple[str, ...]
    required: tuple[str, ...]
    checks: tuple[tuple[str, str], ...]


CASES = (
    CodingCase(
        "discount-bug",
        "Fix discounted_price in pricing.py: apply a percent discount, accept 0 and 100, "
        "and raise ValueError for a percent below 0 or above 100. Keep its signature and tests unchanged.",
        {"pricing.py": "def discounted_price(price, percent):\n    return price * (1 + percent / 100)\n",
         "test_pricing.py": "from pricing import discounted_price\n\ndef test_discount():\n    assert discounted_price(100, 20) == 80\n",
         "pytest.ini": "[pytest]\n"},
        ("pricing.py",), ("pricing.py",),
        (("normal", "from pricing import discounted_price as f\nassert f(100,20)==80"),
         ("bounds", "from pricing import discounted_price as f\nassert f(37,0)==37 and f(37,100)==0"),
         ("negative", "from pricing import discounted_price as f\ntry: f(10,-1)\nexcept ValueError: pass\nelse: raise AssertionError('negative accepted')"),
         ("above100", "from pricing import discounted_price as f\ntry: f(10,101)\nexcept ValueError: pass\nelse: raise AssertionError('above100 accepted')")),
    ),
    CodingCase(
        "tag-feature",
        "Implement parse_tags(text) in tags.py. Split comma-separated text, strip whitespace, "
        "lowercase, remove blank entries and duplicates, and return tags sorted alphabetically. "
        "Keep render_title and the tests unchanged.",
        {"tags.py": "def render_title(value):\n    return value.title()\n\ndef parse_tags(text):\n    raise NotImplementedError\n",
         "test_tags.py": "from tags import parse_tags, render_title\n\ndef test_tags():\n    assert parse_tags('B,a') == ['a','b']\n\ndef test_title():\n    assert render_title('hello') == 'Hello'\n",
         "pytest.ini": "[pytest]\n"},
        ("tags.py",), ("tags.py",),
        (("split_sort", "from tags import parse_tags as f\nassert f('z,b,a')==['a','b','z']"),
         ("dedup_case", "from tags import parse_tags as f\nassert f(' B,b, A ,a')==['a','b']"),
         ("empty", "from tags import parse_tags as f\nassert f(' , ,')==[] and f('')==[]"),
         ("unchanged_api", "from tags import render_title\nassert render_title('hello world')=='Hello World'")),
    ),
    CodingCase(
        "cross-file-rename",
        "Rename legacy_total to total across totals.py, report.py and test_totals.py, updating imports "
        "and calls. Preserve render(values) behavior. Do not retain a legacy_total alias or any references.",
        {"totals.py": "def legacy_total(values):\n    return sum(values)\n",
         "report.py": "from totals import legacy_total\n\ndef render(values):\n    return str(legacy_total(values))\n",
         "test_totals.py": "from totals import legacy_total\nfrom report import render\n\ndef test_total():\n    assert legacy_total([2,3])==5\n\ndef test_render():\n    assert render([2,3])=='5'\n",
         "pytest.ini": "[pytest]\n"},
        ("totals.py", "report.py", "test_totals.py"), ("totals.py", "report.py", "test_totals.py"),
        (("new_api", "from totals import total\nassert total([2,3])==5 and total([])==0"),
         ("caller", "from report import render\nassert render([-2,5])=='3' and render([])=='0'"),
         ("no_alias", "import totals\nassert not hasattr(totals,'legacy_total')"),
         ("no_old_references", "from pathlib import Path\nassert not any('legacy_total' in p.read_text() for p in Path('.').glob('*.py'))")),
    ),
    CodingCase(
        "zero-inventory",
        "Create inventory.py with an Inventory class: new instances are empty; add(name,count) adds "
        "nonnegative integer counts to that name and raises ValueError for negative counts; "
        "total() returns the sum of all counts. Instances must be independent. Create test_inventory.py "
        "with unittest coverage for normal, zero and negative counts, and run the tests. Create only these two files.",
        {}, ("inventory.py", "test_inventory.py"), ("inventory.py", "test_inventory.py"),
        (("add", "from inventory import Inventory\na=Inventory(); a.add('x',2); a.add('x',3); a.add('y',1); assert a.total()==6"),
         ("empty_zero", "from inventory import Inventory\na=Inventory(); assert a.total()==0; a.add('x',0); assert a.total()==0"),
         ("negative", "from inventory import Inventory\na=Inventory()\ntry: a.add('x',-1)\nexcept ValueError: pass\nelse: raise AssertionError('negative accepted')\nassert a.total()==0"),
         ("isolated", "from inventory import Inventory\na=Inventory(); b=Inventory(); a.add('x',1); assert b.total()==0")),
    ),
    CodingCase(
        "zero-slug",
        "Create slug.py with slugify(text): strip whitespace, lowercase, replace every sequence of "
        "characters outside ASCII a-z and 0-9 with one hyphen, and strip leading/trailing hyphens. "
        "Empty or punctuation-only input returns an empty string. Create test_slug.py using unittest "
        "covering normal and edge inputs, run the tests, and create only these two files.",
        {}, ("slug.py", "test_slug.py"), ("slug.py", "test_slug.py"),
        (("normal", "from slug import slugify as f\nassert f(' Hello World ')== 'hello-world'"),
         ("punctuation", "from slug import slugify as f\nassert f('A__B...C')=='a-b-c' and f(\"That's cool\")== 'that-s-cool'"),
         ("empty", "from slug import slugify as f\nassert f('')=='' and f('!!!')==''"),
         ("ascii", "from slug import slugify as f\nassert f('CAFÉ 42')=='caf-42'")),
    ),
)

BY_NAME = {case.name: case for case in CASES}
