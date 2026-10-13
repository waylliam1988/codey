"""A large statement score must not hide untested control-flow branches."""
import pytest

from tools.coverage_gate import check, require_full_production_tree


@pytest.mark.parametrize("lines,branches,passed", [(99, 74, False), (84, 90, False), (85, 75, True)])
def test_gate_requires_both_statement_and_branch_minima(lines, branches, passed):
    report = {"meta": {"branch_coverage": True}, "files": {"codey/example.py": {}},
              "totals": {"num_statements": 100, "num_branches": 100,
                         "covered_lines": lines, "covered_branches": branches}}
    assert check(report, statements=85, branches=75) is passed


@pytest.mark.parametrize("report", [
    {}, {"meta": {"branch_coverage": False}, "files": {"codey/example.py": {}}},
    {"meta": {"branch_coverage": True}, "files": {}},
    {"meta": {"branch_coverage": True}, "files": {"codey/example.py": {}},
     "totals": {"num_statements": 100, "num_branches": 0}},
])
def test_missing_or_statement_only_measurement_is_not_a_green_gate(report):
    with pytest.raises(ValueError):
        check(report, statements=85, branches=75)


def test_unimported_modules_cannot_disappear_from_denominator(tmp_path):
    production = tmp_path / 'codey'
    production.mkdir()
    (production / 'executed.py').touch()
    (production / 'unimported.py').touch()
    report = {'files': {'codey\\executed.py': {}}}
    with pytest.raises(ValueError, match='unimported.py'):
        require_full_production_tree(report, tmp_path)
    report['files']['codey\\unimported.py'] = {}
    require_full_production_tree(report, tmp_path)


def test_tests_only_coverage_cannot_pass_as_production(tmp_path):
    (tmp_path / 'codey').mkdir()
    (tmp_path / 'codey' / 'main.py').touch()
    with pytest.raises(ValueError, match='production codey tree'):
        require_full_production_tree({'files': {'tests/test_main.py': {}}}, tmp_path)
