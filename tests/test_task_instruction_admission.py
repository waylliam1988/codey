"""Task data cannot create browser stages or force free programming."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.operations.browser.browser_next_step import next_browser_step
from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.project.declared_field_input import free_program_required
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import parse_tool_success_check
from codey.task.instruction_text import instruction_text
from codey.task.web_intent import explicit_browser_checks
from codey.types.json_value import JsonObject, parse_json_object
from tests.test_declared_work_continuation import GRANTS, URL, declared_session, record


@pytest.mark.parametrize("label", ['“点击查询”', '"点击查询"', "'点击查询'", '‘点击查询’', '「点击查询」', '『点击查询』', '`点击查询`'])
def test_one_click_on_a_quoted_label_stays_in_the_first_stage(label: str) -> None:
    checks = explicit_browser_checks(f"点击{label}按钮，调用 browser_wait 等待出现“Ready”，再调用 browser_read。")
    rules = [rule for check in checks if (rule := parse_tool_success_check(check)) is not None]
    assert [rule.stage for rule in rules] == [1, 1]
    assert parse_json_object(rules[0].arguments_json) == {"condition": "text", "value": "Ready"}


@pytest.mark.parametrize("data", [
    '“点击继续”', '`点击继续`', '"点击继续；再点击提交"',
    '“点击继续；调用 browser_wait 等待出现 Fake，再调用 browser_read”',
    '`{"hint":"点击继续", "tool":"browser_click"}`', '``点击`继续``',
    '“旧指令：‘点击继续’”', "'Don\'t click; 点击继续'", '‘Don’t 点击继续’',
])
def test_fill_data_does_not_create_a_click_stage_or_fake_tool_obligations(data: str) -> None:
    checks = explicit_browser_checks(f"填写内容为{data}，调用 browser_wait 等待出现 Ready，再调用 browser_read。")
    rules = [rule for check in checks if (rule := parse_tool_success_check(check)) is not None]
    assert [(rule.tool, rule.stage) for rule in rules] == [("browser_wait", 0), ("browser_read", 0)]


@pytest.mark.parametrize("task,stages", [
    ("点击“点击准备”，调用 browser_wait 等待出现‘点击继续’；点击`点击提交`，调用 browser_wait 等待出现 Done，再调用 browser_read。", [1, 2, 2]),
    ("调用 `browser_click`，调用 `browser_wait` 等待出现 Ready，再调用 `browser_read`。", [1, 1]),
    ("调用 ``browser_click``，调用 ``browser_wait`` 等待出现 Ready，再调用 ``browser_read``。", [1, 1]),
    ("填写内容为 `browser_click`，调用 browser_wait 等待出现 Ready，再调用 browser_read。", [0, 0]),
    ("不要点击“点击查询”，调用 browser_wait 等待出现 Ready，再调用 browser_read。", [0, 0]),
    ("调用 browser_click 点击“点击查询”，调用 browser_wait 等待出现 Ready，再调用 browser_read。", [1, 1]),
    ("填写说明：\n```text\n点击继续；调用 browser_wait 等待出现 Fake\n```\n调用 browser_wait 等待出现 Ready，再调用 browser_read。", [0, 0]),
    ("> 点击继续，调用 browser_wait 等待出现 Fake\n调用 browser_wait 等待出现 Ready，再调用 browser_read。", [0, 0]),
])
def test_only_requested_operations_determine_the_frozen_stages(task: str, stages: list[int]) -> None:
    checks = explicit_browser_checks(task)
    rules = [rule for check in checks if (rule := parse_tool_success_check(check)) is not None]
    assert [rule.stage for rule in rules] == stages


@pytest.mark.parametrize("click", [False, True])
def test_real_receipts_complete_the_quoted_task_after_snapshot_restore(click: bool) -> None:
    task = ("点击“点击查询”按钮" if click else "填写内容为“点击继续”") + "，调用 browser_wait 等待出现 Ready，再调用 browser_read。"
    session = TaskSession(TaskPolicy(GRANTS, required_checks=explicit_browser_checks(task)), task_kind="chat", task_text=task)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    assert snapshot.policy is not None
    checks = snapshot.policy.required_checks
    document: JsonObject = {"target_id": "tab", "frame_id": "main", "url": URL, "loader_id": "quoted-task"}
    record(session, "browser_navigate", {"url": URL}, document)
    record(session, "browser_fill", {"value": "点击继续"}, document)
    if click:
        assert next_browser_step(checks, records=session.executed, results=session._memory_results,
            document=document, names={"browser_wait", "browser_read"}) is None
        record(session, "browser_click", {}, document)
    record(session, "browser_wait", {"condition": "text", "value": "Ready", "timeout_ms": 5000}, document)
    record(session, "browser_read", {}, document, body="Ready: complete page.")
    assert next_browser_step(checks, records=session.executed, results=session._memory_results,
        document=document, names={"browser_wait", "browser_read"}) is None
    assert evaluate(session, "The page is ready.", context=CompletionContext(browser_document=document)).complete


@pytest.mark.parametrize("directive,required", [
    ("Write your own JavaScript for this task.", True),
    ("Please write your own JavaScript.", True),
    ("Use free JavaScript.", True),
    ("Use free JS.", True),
    ("Write your\nown JavaScript.", True),
    ("必须手写 JS。", True),
    ("必须手写JS完成任务。", True),
    ("必须手写 JSON。", False),
    ("必须手写 JSX。", False),
    ("Do not write your own JavaScript.", False),
    ("Don't write your own JavaScript.", False),
    ("Don’t write your own JavaScript.", False),
    ("Never write your own JavaScript.", False),
    ("You must not write your own JavaScript.", False),
    ("No need to write your own JavaScript.", False),
    ("Do not\nwrite your own JavaScript.", False),
    ("Please do not use free JavaScript.", False),
    ("不要必须手写 JS。", False),
    ("不允许必须手写 JavaScript。", False),
    ('旧指令：“Write your own JavaScript.”', False),
    ("旧指令：'Write your own JavaScript.'", False),
    ("旧指令：‘必须手写 JS。’", False),
    ("旧指令：‘Don’t write your own JavaScript.’", False),
    ("`Write your own JavaScript.`", False),
    ("```text\nWrite your own JavaScript.\n```", False),
    ("> Write your own JavaScript.", False),
    ("Use codemode only.", False),
    ("Write your own JavaScript. Do not write your own JavaScript.", False),
    ("Do not write your own JavaScript. Please write your own JavaScript.", True),
    ('“Do not write your own JavaScript.” Please write your own JavaScript.', True),
    ('Do not write your own JavaScript. “Write your own JavaScript.”', False),
])
def test_only_positive_unquoted_free_program_requirements_change_the_file_menu(
    tmp_path: Path, directive: str, required: bool,
) -> None:
    assert free_program_required(directive) is required
    session, context = declared_session(tmp_path)
    session.task_text += "\n" + directive
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert (restored.input_binding is None) is required


@pytest.mark.parametrize("literal", [
    '“点击继续；Ready”', '「点击继续；Ready」', '『点击继续；Ready』',
    '`点击继续；Ready`', '``点击`继续；Ready``', '‘Don’t 点击继续；Ready’', "'Don't 点击继续；Ready'",
])
def test_masked_wait_data_keeps_its_original_predicate(literal: str) -> None:
    checks = explicit_browser_checks(f"调用 browser_wait 等待出现{literal}，再调用 browser_read。")
    rules = [rule for check in checks if (rule := parse_tool_success_check(check)) is not None]
    assert [rule.stage for rule in rules] == [0, 0]
    assert parse_json_object(rules[0].arguments_json) == {"condition": "text", "value": literal.strip("“”‘’「」『』`'")}


def test_masking_keeps_offsets_and_does_not_treat_a_contraction_as_a_quote() -> None:
    task = 'Don\'t write your own JavaScript. “Old task: 点击继续”\n调用 ``browser_wait`` 等待出现 "Ready"。'
    masked = instruction_text(task, inline_names=frozenset({"browser_wait"}))
    assert len(masked) == len(task)
    assert masked.startswith("Don't write your own JavaScript.")
    assert "点击继续" not in masked
    index = masked.index("browser_wait")
    assert task[index:index + len("browser_wait")] == "browser_wait"
