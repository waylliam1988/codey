"""NUL Git paths never treat literal arrows or tabs as rename syntax."""
import pytest

from codey.workspace.change_set import ChangeSet
from codey.workspace.changes import _merge_numstat, parse_git_status_nul


@pytest.mark.parametrize("target,source", [("new -> name.py", "old.py"), ("new.py", "old\tname.py")])
def test_nul_rename_target_and_source_are_exact(target, source):
    files = parse_git_status_nul(f"R  {target}\0{source}\0")
    assert files[0]["path"] == target
    assert files[0]["previous_path"] == source
    changes = ChangeSet.from_changes({"ok": True, "files": files, "diff": ""})
    assert changes.files[0].path == target


def test_nul_numstat_literal_arrow_remains_one_path():
    stats = {}
    _merge_numstat(stats, "1\t2\ta => b.py\0")
    assert stats == {"a => b.py": {"additions": 1, "deletions": 2}}


def test_nul_rename_without_source_is_rejected():
    with pytest.raises(ValueError):
        parse_git_status_nul("R  new.py\0")
