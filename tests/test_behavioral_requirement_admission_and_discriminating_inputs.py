"""Explicit requirement admission, not model-selected reference semantics."""
import string

import pytest

from codey.completion.behavioral_checks import admit_behavioral_plan


@pytest.mark.parametrize('phrase', ['removes ASCII punctuation', 'remove ASCII punctuation',
                                  'removing ASCII punctuation'])
def test_explicit_unqualified_deletion_freezes_task_target_and_all_punctuation_pairs(phrase):
    task = f'Fix names.py so clean_name {phrase}, preserves digits and collapses whitespace.'
    plan = admit_behavioral_plan(task, (('names.py', 'clean_name'),))
    assert plan is not None
    assert plan.requirement_quote == phrase
    assert (plan.path, plan.function) == ('names.py', 'clean_name')
    assert len(plan.pairs) == len(string.punctuation)
    for original, inserted in plan.pairs:
        assert inserted.translate(str.maketrans('', '', string.punctuation)) == original
        assert original != inserted and ' ' not in inserted
    assert plan == admit_behavioral_plan(task, (('names.py', 'clean_name'),))
    assert plan.digest != admit_behavioral_plan(task + ' Preserve case.', (('names.py', 'clean_name'),)).digest


@pytest.mark.parametrize('task', [
    'Do not remove ASCII punctuation from clean_name in names.py.',
    'clean_name in names.py should remove ASCII punctuation only at the end.',
    'clean_name in names.py should remove ASCII punctuation except periods.',
    'clean_name in names.py should remove leading ASCII punctuation.',
    'clean_name in names.py should remove ASCII punctuation and replace punctuation with spaces.',
    'clean_name in names.py has a punctuation defect. Fix it.',
    'clean_name in names.py should treat punctuation as word boundaries.',
    "Don't remove ASCII punctuation in clean_name in names.py.",
    'clean_name in names.py removes ASCII punctuation when mode is enabled.',
    'clean_name in names.py removes ASCII punctuation if requested.',
    'clean_name is defined in names.py. A different operation removes ASCII punctuation.',
    'The old specification said "clean_name removes ASCII punctuation". Keep current behavior.',
    'Does clean_name in names.py remove ASCII punctuation?',
    'clean_name in names.py removes ASCII punctuation on weekends.',
    'clean_name in names.py removes ASCII punctuation from the suffix.',
    'clean_name in names.py removes ASCII punctuation, then replaces it with spaces.',
])
def test_negation_qualification_conflict_and_unspecified_semantics_are_not_admitted(task):
    assert admit_behavioral_plan(task, (('names.py', 'clean_name'),)) is None


def test_only_unique_task_bound_target_is_admitted_without_assuming_function_names():
    task = 'Verify remove ASCII punctuation in clean_name.'
    assert admit_behavioral_plan(task, (('names.py', 'clean_name'), ('other.py', 'clean_name'))) is None
    assert admit_behavioral_plan(task, (('names.py', 'unrelated'),)) is None
    plan = admit_behavioral_plan(task, (('names.py', 'clean_name'), ('other.py', 'unrelated')))
    assert plan is not None and plan.function == 'clean_name'


def test_property_distinguishes_deletion_from_space_substitution_without_full_expected_output():
    plan = admit_behavioral_plan('Fix names.py: clean_name removes ASCII punctuation.', (('names.py', 'clean_name'),))
    assert plan is not None
    def delete(text):
        return '-'.join(text.translate(str.maketrans('', '', string.punctuation)).lower().split())
    def substitute(text):
        return '-'.join(''.join(' ' if c in string.punctuation else c for c in text).lower().split())
    assert all(delete(a) == delete(b) for a, b in plan.pairs)
    assert any(substitute(a) != substitute(b) for a, b in plan.pairs)


def test_no_unrequested_unicode_constraint_or_model_expected_output_is_introduced():
    plan = admit_behavioral_plan('Fix names.py: clean_name removes ASCII punctuation and lowercases ASCII letters.',
                                 (('names.py', 'clean_name'),))
    assert plan is not None
    assert all(a.isascii() and b.isascii() for a, b in plan.pairs)
    assert not hasattr(plan, 'expected')


def test_multi_function_task_binds_deletion_to_its_own_clause_not_later_function():
    task = ('Fix names.py so clean_name removes ASCII punctuation and preserves digits. '
            'and adapter.py display_name to wrap the normalized name in [].')
    plan = admit_behavioral_plan(task, (('names.py', 'clean_name'), ('adapter.py', 'display_name')))
    assert plan is not None
    assert (plan.path, plan.function) == ('names.py', 'clean_name')


def test_edit_scope_in_later_sentence_does_not_qualify_the_behavior_requirement():
    task = ('Fix names.py so clean_name removes ASCII punctuation and collapses whitespace into hyphens. '
            'Do not modify tests. Inspect tests, edit only names.py, and run the tests.')
    assert admit_behavioral_plan(task, (('names.py', 'clean_name'),)) is not None


@pytest.mark.parametrize('task', [
    'Fix names.py normalization (lowercase, remove ASCII punctuation, preserve digits).',
    'Under special conditions clean_name in names.py removes ASCII punctuation.',
    'clean_name in names.py removes ASCII punctuation, provided the mode is normal.',
])
def test_unbound_or_outside_closed_requirement_forms_remain_unadmitted(task):
    assert admit_behavioral_plan(task, (('names.py', 'clean_name'),)) is None


@pytest.mark.parametrize('limitation', ['Preserve periods.', 'Keep hyphens.',
    'Except when processing identifiers.', 'Do that only for ASCII-only input.'])
def test_later_explicit_exceptions_do_not_admit_an_unqualified_deletion_rule(limitation):
    task = 'Fix names.py so clean_name removes ASCII punctuation. ' + limitation
    assert admit_behavioral_plan(task, (('names.py', 'clean_name'),)) is None
