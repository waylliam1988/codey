"""Pi's real cut and split-turn summary planning remain atomic in replay."""
from pathlib import Path

from tools.context_compaction_benchmark.pi import select_pi

ROOT = Path(__file__).resolve().parent / "fixtures" / "pi_compaction_reference"


def history():
    return [{"role": "user", "content": "Keep API"},
            {"role": "assistant", "content": "old investigation " * 2000},
            {"role": "assistant", "tool_calls": [{"id":"a", "function":{"name":"read", "arguments":'{"path":"app.py"}'}}]},
            {"role": "tool", "tool_call_id":"a", "content":"recent result " * 300}]


def test_actual_pi_cut_never_starts_at_an_unpaired_tool_result():
    result = select_pi(ROOT, history(), keep_tokens=100)
    assert result["cut"] == 2
    assert result["split"] is True
    assert result["plans"]
    assert "Original Request" in result["plans"][-1]["messages"][0]["content"][0]["text"]


def test_actual_pi_tracks_read_files_in_its_checkpoint():
    items = history() + [{"role":"user", "content":"continue"}]
    result = select_pi(ROOT, items, keep_tokens=1)
    assert "app.py" in result["summary"]
    assert "## Goal" in result["plans"][0]["messages"][0]["content"][0]["text"]


def test_actual_pi_can_plan_both_history_and_split_turn_summaries():
    items = [{'role':'user', 'content':'first task'}, {'role':'assistant','content':'finished old work ' * 500},
             {'role':'user','content':'current task'}, *history()[1:]]
    result = select_pi(ROOT, items, keep_tokens=100)
    assert result['split']
    assert len(result['plans']) == 2
