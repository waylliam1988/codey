from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path
from typing import Protocol
from unittest.mock import Mock

import pytest

from codey.app.context import AppContext
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.model import ArtifactRecord
from codey.durable.provider_response import read_response_artifact
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.write.mutation_line import RuntimeMutationLine
from tests.support.provider_actions import ProviderActions


class Signal(Protocol):
    def set(self) -> None: ...


def delete_in_another_process(path: Path, started: Signal, deleted: Signal) -> None:
    session = DurableSession(SQLiteDurableStore(path))
    try:
        started.set()
        session.delete_conversation("chat")
        deleted.set()
    finally:
        session.close()


def test_cross_process_delete_waits_for_the_reading_process(tmp_path: Path) -> None:
    path = tmp_path / "cross-process.sqlite"
    session = DurableSession(SQLiteDurableStore(path))
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "read"})
    context = multiprocessing.get_context("spawn")
    started, deleted = context.Event(), context.Event()
    worker = context.Process(target=delete_in_another_process, args=(path, started, deleted))
    try:
        with session.history_read("chat") as reader:
            worker.start()
            assert started.wait(10)
            assert not deleted.wait(.2)
            assert reader.display_head("chat")[1] == 1
        assert deleted.wait(10)
        worker.join(10)
        assert worker.exitcode == 0
        assert not session.has_conversation("chat")
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        session.close()


def test_nested_control_read_rejects_admission_during_delete_instead_of_deadlocking(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "lock-order.sqlite"))
    session.create_conversation("chat")
    exclusive = threading.Event()
    completed = threading.Event()

    def delete() -> None:
        with session.store.history_lifetime.exclusive():
            exclusive.set()
            session.delete_conversation("chat")
        completed.set()

    worker = threading.Thread(target=delete)
    try:
        with session.control_line():
            worker.start()
            assert exclusive.wait(5)
            with pytest.raises(ValueError, match="history snapshot changed"):
                session.history_page("chat")
        assert completed.wait(5)
    finally:
        worker.join(5)
        session.close()


def test_conversation_cleanup_waits_for_selected_provider_file_and_finishes_physical_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.durable import provider_response

    ctx = AppContext(tmp_path, start_scheduler=False)
    durable = ctx.durable
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="inspect", provider_snapshot={})
    durable.claim_task("r")
    durable.start_task("r", state=dict(durable.task("r").state))
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4, max_repair_rounds=1)
    mutations.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
                            managed_outputs=ctx.managed_outputs)
    task = TaskSession(TaskPolicy(frozenset({"project.read", "control"})), max_turns=4)
    sink.bind_request(build_turn_snapshot(task, native=True), logical_turn=1)
    reply = AssistantTurn(text="tool body", tool_calls=(ProviderToolCall("one", "read_file", {"path": "a.py"}),))
    ProviderActions(Mock(window_ref=None, native_tools=False, send=Mock(return_value=reply)), sink).send("inspect")
    entered, release, deleted = threading.Event(), threading.Event(), threading.Event()
    paths: list[Path] = []
    errors: list[BaseException] = []
    pages: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    def read_file(record: ArtifactRecord) -> object:
        paths.append(Path(record.storage_path))
        entered.set()
        assert release.wait(10)
        return read_response_artifact(record)

    def read() -> None:
        try:
            pages.append(durable.history_page("s"))
        except BaseException as error:
            errors.append(error)

    def delete() -> None:
        try:
            failures.append(ctx.conversation_coordinator.forget("s", preserve_ui=True))
            deleted.set()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(provider_response, "read_response_artifact", read_file)
    reader, deleter = threading.Thread(target=read), threading.Thread(target=delete)
    try:
        reader.start()
        assert entered.wait(5)
        deleter.start()
        assert not deleted.wait(.2)
        assert paths[0].is_file()
        # Deletion is waiting outside the session line.
        with durable.control_line():
            assert paths[0].is_file()
    finally:
        release.set()
        reader.join(10)
        if deleter.ident is not None:
            deleter.join(10)
        ctx.close()
    assert not errors
    assert failures == [{}]
    assert deleted.is_set()
    assert not paths[0].exists()
    assert pages[0]["items"][-1]["tool_calls"][0]["id"] == "one"
