from pathlib import Path

import pytest

from codey.app.api import run_submit_response
from codey.providers import model_preferences
from codey.utils.object_fields import sequence_items


def test_typed_input_uses_its_actual_text_for_readonly_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_preferences, "DEFAULT_STATE_HOME", tmp_path / "state")
    submissions: list[dict[str, object]] = []

    def submit(*args: object, **kwargs: object) -> str:
        submissions.append(kwargs)
        return "run_" + "a" * 32

    status, _ = run_submit_response({"project": str(tmp_path), "intent": "project", "provider": "deepseek",
        "input": {"kind": "start_task", "text": "只读分析，不要修改文件，不要运行命令。"}}, submit)
    assert status == 200
    assert submissions[0]["project_changes_required"] is False
    denied = submissions[0]["denied_capabilities"]
    assert sequence_items(denied) and "project.write" in denied
