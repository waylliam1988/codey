import pytest

from codey.workspace.task_changes import collect_task_changes
from tests.manual.cross_model_quality_cases import FIXTURE_BY_NAME
from tests.manual.cross_model_quality_controls import seed_candidate


@pytest.mark.parametrize("name", ["config-isolation", "atomic-transfer"])
@pytest.mark.parametrize("flawed", [False, True])
def test_calibration_always_presents_a_real_candidate_diff(tmp_path, name, flawed):
    fixture = FIXTURE_BY_NAME[name]
    tracker = seed_candidate(tmp_path, fixture, flawed)
    changes = collect_task_changes(tmp_path, tracker)
    assert changes["files"]
    expected = fixture.mutants[0] if flawed else fixture.reference
    for path, text in expected.items():
        assert (tmp_path / path).read_text(encoding="utf-8") == text
