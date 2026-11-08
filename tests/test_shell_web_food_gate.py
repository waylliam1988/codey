"""Food gate requires opened page text, not snippets or model self-report."""
from __future__ import annotations

import json

import pytest

from tools.shell_web_food_gate import analyze


def evidence(tmp_path, *, body="鱼丸、肉燕、锅边糊", protocol="native", source="https://example.com/article"):
    summary = {"foods": ["鱼丸", "肉燕", "锅边糊"], "sources": [source], "reason": "正文推荐"}
    events = [
        {"type": "tool", "tool_name": "web_search", "ok": True, "path": "福州美食"},
        {"type": "tool", "tool_name": "open_url", "ok": True, "path": "https://example.com/article"},
        {"type": "task_done", "stop_reason": "done", "summary": json.dumps(summary, ensure_ascii=False)},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
    opened = ("Title: 食物介绍\nURL: https://example.com/article\n" + body
              + '\nResult metadata:\n{"tool":"open_url","ok":true}')
    snippet = '搜索摘要：鱼丸、肉燕、锅边糊\nResult metadata:\n{"tool":"web_search","ok":true}'
    messages = ([{"role": "tool", "content": snippet}, {"role": "tool", "content": opened}]
                if protocol == "native" else
                [{"role": "user", "content": "[result: web_search]\n" + snippet + "\n[result: open_url]\n" + opened}])
    (tmp_path / "provider.jsonl").write_text(json.dumps({"type": "request", "payload": {"messages": messages}}),
                                             encoding="utf-8")


@pytest.mark.parametrize("protocol", ["native", "compatibility"])
def test_opened_food_names_and_sources_pass(tmp_path, protocol):
    evidence(tmp_path, protocol=protocol)
    assert analyze(tmp_path)["ok"] is True


@pytest.mark.parametrize("protocol", ["native", "compatibility"])
def test_snippet_cannot_substitute_for_page_body(tmp_path, protocol):
    evidence(tmp_path, body="页面只描述当地交通", protocol=protocol)
    report = analyze(tmp_path)
    assert report["food_names_in_delivered_page_body"] is False
    assert report["ok"] is False


def test_fabricated_source_is_rejected(tmp_path):
    evidence(tmp_path, source="https://invented.example.com/")
    assert analyze(tmp_path)["sources_match_opened_pages"] is False


def test_multiple_delivered_passages_of_same_source_are_combined(tmp_path):
    evidence(tmp_path, body="鱼丸、肉燕")
    path = tmp_path / "provider.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["payload"]["messages"].append({"role": "tool", "content":
        'Title: 第二段\nURL: https://example.com/article\n锅边糊\nResult metadata:\n{"tool":"open_hit","ok":true}'})
    path.write_text(json.dumps(row), encoding="utf-8")
    assert analyze(tmp_path)["ok"] is True
