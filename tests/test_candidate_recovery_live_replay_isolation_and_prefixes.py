"""Boundaries of the local continuation experiment, not model success claims."""
import pytest

from tests.manual.candidate_recovery_koboldcpp_worker import replay_steps


def test_live_project_is_outside_any_git_worktree_including_parent_repository(tmp_path):
    from tests.manual.candidate_recovery_koboldcpp_ab import isolated_project

    root = isolated_project('native', 'proposal')
    assert not any((folder / '.git').exists() for folder in (root, *root.parents))




@pytest.mark.parametrize('kind,tail', [('stale-search-replay', ['edit'] * 3),
                                    ('counterexample-repair-replay', ['run', 'done'])])
def test_recorded_boundary_prefix_is_explicit_and_does_not_generate_expected_outputs(kind, tail):
    driver = replay_steps(kind)
    names = [name for name, _ in driver.steps]
    assert names[:4] == ['run', 'read_file', 'read_file', 'edit']
    assert names[-len(tail):] == tail
    assert all(name != 'shell' for name in names)
    assert driver.native_tools is True



def test_unknown_replay_is_not_silently_reclassified():
    with pytest.raises(ValueError, match='Unknown replay'):
        replay_steps('unknown')
