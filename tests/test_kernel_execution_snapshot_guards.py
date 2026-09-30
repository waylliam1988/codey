"""Execution and parsing cannot widen a captured turn's authority."""

from dataclasses import replace

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall


def test_incomplete_snapshot_is_rejected_without_live_registry_fallback():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})))
    snapshot = replace(build_turn_snapshot(session), frozen_specs=())
    plan = normalize_turn('{"tool":"web_search","args":{"query":"q"}}', snapshot=snapshot)
    assert plan.protocol_error
    assert not plan.calls


def test_snapshot_cannot_accept_another_policy():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})))
    snapshot = build_turn_snapshot(session)
    plan = normalize_turn('{"tool":"web_search","args":{"query":"q"}}',
                          snapshot=snapshot, policy=TaskPolicy(grants=frozenset({"control", "web.read", "project.write"})))
    assert plan.protocol_error
    assert not plan.calls


def test_executor_rechecks_snapshot_availability_before_effect_intents():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True))
    snapshot = build_turn_snapshot(session)
    invoked = []

    class Sink:
        def begin_turn(self, *args, **kwargs):
            invoked.append("intent")

        def has_unsettled(self, identity):
            return False

        def settle(self, *args, **kwargs):
            invoked.append("settlement")

    results = execute_turn(session, [ToolCall("open_url", {"url":"https://example.com"})],
                           snapshot=snapshot, intent_sink=Sink(),
                           executors={"open_url": lambda _: invoked.append("open")})
    assert not invoked
    assert len(results) == 1
    assert results[0].model_text.startswith("ERROR:")


def test_executor_rechecks_parameters_before_effect_intents():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})))
    invoked = []
    results = execute_turn(session, [ToolCall("web_search", {"query":42})],
                           snapshot=build_turn_snapshot(session),
                           executors={"web_search": lambda _: invoked.append("search")})
    assert not invoked
    assert results[0].model_text.startswith("ERROR:")


def test_large_number_cannot_bypass_maximum():
    from codey.toolchain.tool_spec import ToolSpec, validate_args_with_spec

    spec = ToolSpec(name="numeric_probe", executor="custom", grant="control",
                    parameters=(("value", {"type":"number", "maximum":10}),))
    assert validate_args_with_spec(spec, {"value":10**1000})
