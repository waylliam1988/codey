"""Provider workflow paths must share deferred generation settlement."""

from __future__ import annotations

import ast
from pathlib import Path

from codey.operations.generation_boundary import durable_generation_boundary_callback

ROOT = Path(__file__).parents[2]


class _Submission:
    content = {"text": "steer this turn"}


class _Durable:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def has_conversation(self, session_id: str) -> bool:
        return session_id == "session"

    def commit_generation_boundary(self, session_id: str, *, task_id: str, at: str) -> tuple[object, ...]:
        self.calls.append((task_id, at))
        return (_Submission(),)


class _Mutations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, str]] = []

    def settle_provider_effect_at_boundary(
        self,
        session_id: str,
        run_id: str,
        settlement: object,
        *,
        boundary: str,
        messages: object = (),
    ) -> tuple[object, ...]:
        self.calls.append((session_id, run_id, settlement, boundary))
        return (_Submission(),)


class _FailingMutations(_Mutations):
    def settle_provider_effect_at_boundary(
        self, session_id: str, run_id: str, settlement: object, *, boundary: str, messages: object = (),
    ) -> tuple[object, ...]:
        raise OSError("injected boundary failure")


class _Sink:
    def __init__(self, mutations: _Mutations) -> None:
        self.mutations = mutations
        self.pending = (object(), ())

    def pending_provider_settlement(self) -> object:
        return self.pending


def test_provider_settlement_and_boundary_use_one_mutation() -> None:
    durable = _Durable()
    mutations = _Mutations()
    sink = _Sink(mutations)

    callback = durable_generation_boundary_callback(
        durable=durable,
        session_id="session",
        task_id="run",
        sink=sink,
    )

    assert callback is not None
    assert callback("post_tools") == "steer this turn"
    assert mutations.calls[0][0:2] == ("session", "run")
    assert mutations.calls[0][3] == "post_tools"
    assert durable.calls == []


def test_boundary_without_pending_settlement_uses_durable_commit() -> None:
    durable = _Durable()
    callback = durable_generation_boundary_callback(
        durable=durable,
        session_id="session",
        task_id="run",
    )

    assert callback is not None
    assert callback("final") == "steer this turn"
    assert durable.calls == [("run", "final")]


def test_failed_boundary_keeps_pending_settlement_for_recovery() -> None:
    durable = _Durable()
    sink = _Sink(_FailingMutations())
    callback = durable_generation_boundary_callback(
        durable=durable, session_id="session", task_id="run", sink=sink,
    )

    assert callback is not None
    try:
        callback("post_tools")
    except OSError as exc:
        assert str(exc) == "injected boundary failure"
    else:
        raise AssertionError("boundary failure should propagate")
    assert sink.pending is not None


def test_all_provider_workflows_attach_the_shared_boundary() -> None:
    for relative in (
        "codey/operations/project_adapter.py",
        "codey/operations/research_iteration.py",
        "codey/operations/evidence_followup.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        sink_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "KernelEffectSink"
        ]
        assert sink_calls, relative
        observation_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "KernelObservationDeps"
        ]
        assert observation_calls, relative
        assert any(any(keyword.arg == "on_boundary" for keyword in call.keywords) for call in observation_calls), (
            relative
        )
