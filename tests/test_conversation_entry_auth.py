from unittest.mock import Mock

from codey.app.api import run_submit_response


def test_typed_input_uses_its_actual_text_for_readonly_authorization(tmp_path):
    submit = Mock(return_value="run_" + "a" * 32)
    status, _ = run_submit_response({"project": str(tmp_path), "intent": "project", "provider": "deepseek",
        "input": {"kind": "start_task", "text": "只读分析，不要修改文件，不要运行命令。"}}, submit)
    assert status == 200
    assert submit.call_args.kwargs["project_changes_required"] is False
    assert "project.write" in submit.call_args.kwargs["denied_capabilities"]
