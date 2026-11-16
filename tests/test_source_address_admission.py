"""Original standalone source addresses enter the existing frozen source contract."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.context import RunWork
from codey.operations.task_entry import build_task_policy_for_entry, start_task_session
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.task.web_intent import explicit_web_intent, restored_source_requirements, source_requirements, source_url_key
from tests.support.task_entry_helpers import entry_frame, submission

URL = 'https://EXAMPLE.COM/Record/Q-572?version=003#detail'
KEY = source_url_key(URL)


@pytest.mark.parametrize('address', [URL, f'<{URL}>', f'URL: {URL}', f'Source: {URL}', f'来源：{URL}',
                                     f'- {URL}', f'1. {URL}', f'[Original record]({URL})'])
def test_standalone_original_addresses_are_sources_without_declaring_browser_actions(address: str) -> None:
    task = 'Q-572 现在是什么状态，何时完成？\n' + address
    intent = explicit_web_intent(task)
    assert intent.urls == {KEY} and intent.original_urls == {URL}
    assert intent.requested and not intent.browser_navigation_requested
    assert source_requirements(task) == {'version': 1, 'required_urls': [KEY]}


@pytest.mark.parametrize('task', [f'`{URL}`', f'```text\n{URL}\n```', f'~~~text\n{URL}\n~~~',
    f'> {URL}', f'"{URL}"', f'“{URL}”', f'请解释这个字符串：{URL}',
    f'不要打开\n{URL}', f'{URL}\nDo not read this page', f'不要联网\n{URL}',
    f'Do not browse\n{URL}', f'Do not visit {URL}.\n问题是什么？\n{URL}',
    f'{URL}\n问题是什么？\nDo not visit {URL}'])
def test_examples_quotes_and_revoked_addresses_do_not_become_required_sources(task: str) -> None:
    assert not explicit_web_intent(task).urls
    assert source_requirements(task)['required_urls'] == []


def test_each_source_address_keeps_its_own_revocation() -> None:
    other = 'https://example.com/other-record'
    task = f'比较两份记录。\n{URL}\n{other}\nDo not visit {URL}'
    assert explicit_web_intent(task).urls == {other}


def test_existing_source_snapshots_and_legacy_tasks_do_not_gain_new_requirements() -> None:
    task = 'Report the record state.\n' + URL
    assert not restored_source_requirements({'input_text': task})
    assert not restored_source_requirements({'input_text': task, 'source_requirements': {'version': 1, 'required_urls': []}})
    assert restored_source_requirements({'input_text': 'updated prompt', 'source_requirements': source_requirements(task)}) == {KEY}


def test_new_source_address_is_frozen_at_admission_and_survives_prompt_changes(tmp_path: Path) -> None:
    durable = DurableSession(SQLiteDurableStore(tmp_path / 'admission.sqlite3'))
    task = 'Report the record state.\n' + URL
    durable.admit_input(conversation_id='s', request_id='q', task_id='r', text=task,
                        provider_snapshot={'provider_id': 'local'})
    frame = entry_frame(replace(submission('resume without an address'), project=None))
    policy = build_task_policy_for_entry(frame.request, 'chat')
    session = start_task_session(frame, RunWork([], ExecutionEvidence()), policy, 'chat', durable=durable)
    assert session.required_source_urls == {KEY}
    assert session.clone_projection().required_source_urls == {KEY}
