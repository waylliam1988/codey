from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.toolchain.runtime import EditBlock, edit_file


def test_edit_detail_comes_from_the_file_content_actually_written(tmp_path):
    (tmp_path / "cache.py").write_text("first\nold\nlast\n", encoding="utf8")
    outcome = edit_file(tmp_path, "cache.py", [EditBlock("old", "new\nadded")])
    payload = run_event_ui_payload("r", "s", RunEvent.tool_finished(1, ToolCall("edit", {"path": "cache.py"}), outcome))
    preview = payload["preview"]
    assert preview["additions"] == 2 and preview["deletions"] == 1
    assert "-old" in preview["diff"] and "+added" in preview["diff"]
    assert preview["diff_truncated"] is False
