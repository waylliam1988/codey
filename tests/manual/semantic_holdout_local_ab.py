# ruff: noqa: E402 -- direct execution loads the existing native comparison harness.
"""New callable/filename tasks and correct-source controls through native agents."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from tests.manual import codey_vs_pi_agent_stability_ab as native
from tests.manual.agent_stability_cases import COMMAND, OBSERVER, ExperimentCase


def holdout_cases():
    visible = ("import unittest\nfrom labels import squash_label\n"
               "class Labels(unittest.TestCase):\n"
               "    def test_label(self): self.assertEqual(squash_label(' City, Council! '), 'city-council')\n")
    shared = {'app.py': 'from labels import squash_label as normalize_name\n',
              'test_labels.py': visible + OBSERVER}
    good = ("import string\ndef squash_label(value):\n"
            "    return '-'.join(value.casefold().translate(str.maketrans('', '', string.punctuation)).split())\n")
    wrong = ("import string\ndef squash_label(value):\n"
             "    return '-'.join(''.join(' ' if c in string.punctuation else c for c in value).lower().split())\n")
    requirement = ('squash_label removes ASCII punctuation, lowercases ASCII letters, '
                   'trims outer whitespace and collapses whitespace into hyphens. Preserve digits. ')
    return (
        ExperimentCase('heldout-fix-labels', 'Fix labels.py so ' + requirement
            + f'Do not modify tests or app.py. Inspect tests, edit only labels.py, run {COMMAND}.',
            {**shared, 'labels.py': wrong}, ('labels.py',)),
        ExperimentCase('heldout-verify-labels', 'Verify labels.py: ' + requirement
            + f'Do not change any file. Run {COMMAND}.', {**shared, 'labels.py': good}, ()),
    )


if __name__ == '__main__':
    raise SystemExit(native.main(cases=holdout_cases()))
