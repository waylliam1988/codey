"""Answer predicates read committed results; execution alone cannot satisfy them."""
from __future__ import annotations

import json

import pytest

from codey.operations.completion_gate import evaluate
from codey.policies.tool_requirement import parse_tool_success_check, require_tool_success
from tests.test_required_tool_lineage import _code, _execute, _restart
from tests.test_required_tool_lineage import runtime as runtime

BINDINGS = tuple(require_tool_success(
    "read_file", via="codemode", arguments={"path": path},
    answer_pointer=f"/{key}", result_pointer="/text", result_line=1,
) for key, path in (("a", "a.txt"), ("b", "b.txt")))


@pytest.mark.parametrize("runtime", [BINDINGS], indirect=True)
@pytest.mark.parametrize("native", [False, True])
def test_committed_reads_reject_first_characters_and_recover_exact_answer(runtime, native, monkeypatch):
    (runtime.project / "a.txt").write_text("AMBER-471\nsecond\n", encoding="utf-8")
    (runtime.project / "b.txt").write_text("COBALT-923\n", encoding="utf-8")
    _execute(runtime, [_code("return await Promise.all(['a.txt','b.txt'].map(path => tools.read_file({path})));" )], native)
    correct = json.dumps({"a": "AMBER-471", "b": "COBALT-923"})
    assert not evaluate(runtime.state, '{"a":"A","b":"C"}').complete
    assert evaluate(runtime.state, correct).complete
    restored = _restart(runtime)
    assert evaluate(restored, correct).complete
    assert not evaluate(restored, '{"a":"A","b":"C"}').complete
    # Completion refers to observed content, not a second filesystem read.
    (runtime.project / "a.txt").write_text("external change\n", encoding="utf-8")
    monkeypatch.setattr("codey.toolchain.runtime.read_file", lambda *_a, **_kw: pytest.fail("completion re-read file"))
    assert evaluate(restored, correct).complete


@pytest.mark.parametrize("runtime", [BINDINGS], indirect=True)
@pytest.mark.parametrize("answer", [
    '{"a":true,"b":"COBALT-923"}', '{"b":"COBALT-923"}',
    '{"a":"wrong","a":"AMBER-471","b":"COBALT-923"}',
    '{"a":NaN,"b":"COBALT-923"}', 'AMBER-471 / COBALT-923',
])
def test_answer_binding_rejects_wrong_types_missing_fields_and_ambiguous_json(runtime, answer):
    (runtime.project / "a.txt").write_text("AMBER-471\n", encoding="utf-8")
    (runtime.project / "b.txt").write_text("COBALT-923\n", encoding="utf-8")
    _execute(runtime, [_code("return await Promise.all(['a.txt','b.txt'].map(path => tools.read_file({path})));" )], True)
    assert not evaluate(runtime.state, answer).complete


@pytest.mark.parametrize("runtime", [BINDINGS], indirect=True)
def test_execution_projection_and_display_cannot_replace_missing_full_receipt(runtime):
    (runtime.project / "a.txt").write_text("AMBER-471\n", encoding="utf-8")
    (runtime.project / "b.txt").write_text("COBALT-923\n", encoding="utf-8")
    _execute(runtime, [_code("return await Promise.all(['a.txt','b.txt'].map(path => tools.read_file({path})));" )], True)
    runtime.state._memory_results.clear()
    assert not evaluate(runtime.state, '{"a":"AMBER-471","b":"COBALT-923"}').complete
    assert evaluate(_restart(runtime), '{"a":"AMBER-471","b":"COBALT-923"}').complete


def test_answer_binding_roundtrips_inside_original_policy_and_check_id():
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(frozenset({"control", "project.read"}), required_checks=BINDINGS)
    assert TaskPolicy.from_payload(policy.to_payload()) == policy
    requirement = parse_tool_success_check(BINDINGS[0])
    assert requirement is not None
    assert len(requirement.check_id) <= 80
    assert "Final answer" in requirement.guidance
    assert "AMBER" not in requirement.declaration


SINGLE = (require_tool_success("read_file", via="codemode", arguments={"path": "a.txt"},
                              answer_pointer="/a~1b/~0key/0", result_pointer="/text", result_line=1),)


@pytest.mark.parametrize("runtime", [SINGLE], indirect=True)
def test_failed_attempt_does_not_poison_later_observation_and_full_receipt_survives_excerpt(runtime):
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    text = "中文" * 350
    (runtime.project / "a.txt").write_text(text + "\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True, start=2)
    answer = json.dumps({"a/b": {"~key": [text]}}, ensure_ascii=False)
    assert evaluate(runtime.state, answer).complete
    assert evaluate(_restart(runtime), answer).complete


@pytest.mark.parametrize("runtime", [SINGLE], indirect=True)
def test_conflicting_committed_observations_cannot_pick_an_arbitrary_answer(runtime):
    for turn, text in enumerate(("before", "after"), 1):
        (runtime.project / "a.txt").write_text(text + "\n", encoding="utf-8")
        _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True, start=turn)
    restored = _restart(runtime)
    for state in (runtime.state, restored):
        for text in ("before", "after"):
            assert not evaluate(state, json.dumps({"a/b": {"~key": [text]}})).complete


@pytest.mark.parametrize("runtime", [SINGLE], indirect=True)
def test_later_pages_do_not_prove_or_invalidate_first_line_observation(runtime):
    (runtime.project / "a.txt").write_text("first\nsecond\n", encoding="utf-8")
    answer = json.dumps({"a/b": {"~key": ["first"]}})
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt',offset:2});")], True)
    assert not evaluate(runtime.state, answer).complete
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt',limit:1});")], True, start=2)
    assert evaluate(runtime.state, answer).complete
    assert evaluate(_restart(runtime), answer).complete


@pytest.mark.parametrize("runtime", [SINGLE], indirect=True)
def test_result_from_other_arguments_cannot_replace_the_committed_receipt(runtime):
    from dataclasses import replace

    from codey.runtime.core.models import ToolCall

    (runtime.project / "a.txt").write_text("observed\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    identity = next(key for key, row in runtime.state.executed.items() if row["name"] == "read_file")
    receipt = runtime.state._memory_results[identity]
    runtime.state._memory_results[identity] = replace(receipt, call=ToolCall("read_file", {"path": "other.txt"}))
    answer = json.dumps({"a/b": {"~key": ["observed"]}})
    assert not evaluate(runtime.state, answer).complete
    assert evaluate(_restart(runtime), answer).complete


@pytest.mark.parametrize("kwargs", [
    {"answer_pointer": "no-root"}, {"answer_pointer": "/bad~2"},
    {"answer_pointer": "/a", "result_line": True, "result_pointer": "/text"},
    {"answer_pointer": "/a", "result_line": 0, "result_pointer": "/text"},
    {"result_pointer": "/text"}, {"answer_pointer": "/a", "result_line": 1},
])
def test_invalid_answer_binding_is_rejected_at_declaration(kwargs):
    with pytest.raises(ValueError):
        require_tool_success("read_file", **kwargs)


@pytest.mark.parametrize("runtime", [BINDINGS], indirect=True)
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mistake", ["first_characters", "extra_quotes", "missing_content_quotes", "markdown_fence"])
def test_wrong_answer_is_repaired_by_original_task_loop_without_repeating_tools(runtime, native, mistake):
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from tests.support.kernel_runtime import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )

    first = '"AMBER-471"' if mistake == "missing_content_quotes" else "AMBER-471"
    (runtime.project / "a.txt").write_text(first + "\n", encoding="utf-8")
    (runtime.project / "b.txt").write_text("COBALT-923\n", encoding="utf-8")
    runtime.state.max_turns = 5
    correct = json.dumps({"a": first, "b": "COBALT-923"}, separators=(",", ":"))
    wrong = ('{"a":"A","b":"C"}' if mistake == "first_characters" else
             json.dumps({"a": json.dumps(first) if mistake == "extra_quotes" else "AMBER-471", "b": "COBALT-923"}))
    if mistake == "markdown_fence":
        wrong = "```json\n" + correct + "\n```"

    class Provider:
        native_tools = native
        step = 0

        def advance(self, feedback):
            index = self.step
            self.step += 1
            if index == 0:
                assert "without Markdown fences" in feedback
                if native:
                    assert "summary string" in feedback
                name, args = "codemode", {"code":
                    "const a = await tools.read_file({path:'a.txt'});"
                    "const b = await tools.read_file({path:'b.txt'});" +
                    ("return {a:a.value.text[0],b:b.value.text[0]};" if mistake == "first_characters" else
                     "return a.value.text.split('\\n')[0];")}
            elif index == 1:
                assert ('"a":"A"' if mistake == "first_characters" else
                        json.dumps({"ok": True, "value": first}, separators=(",", ":"))) in feedback
                name, args = "done", {"summary": wrong}
            else:
                assert index == 2
                assert "Final answer must be JSON" in feedback
                assert "Execution and observation are already satisfied" in feedback
                assert "Required execution:" not in feedback
                if native:
                    assert "done" in feedback and "summary string" in feedback
                    assert "not the outer tool-call object" in feedback
                assert len(runtime.state.executed) == 3
                assert not evaluate(runtime.state, wrong).complete
                name, args = "done", {"summary": correct}
            return (AssistantTurn(tool_calls=(ProviderToolCall(f"call-{index}", name, args),)) if native
                    else json.dumps({"tool": name, "args": args}))

        def send(self, prompt, timeout=None):
            return self.advance(prompt)

        def send_turn(self, prompt, tools=None, timeout=None):
            return self.advance(prompt)

        def send_tool_results(self, results, tools=None, timeout=None):
            return self.advance(results[0].content)

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return AssistantTurn(text=correct)

    provider = Provider()
    result = run_task_kernel(runtime.state, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=provider, run_id="r",
            provider_id="local", effect_scope="task", user_task="Read both files and report their first lines."),
        execution=KernelExecutionDeps(project_path=runtime.project, managed_outputs=runtime.managed, session_id="s",
            durable=runtime.mutations.durable, workspace_revision_store=runtime.revisions,
            permission_profile="planning_readonly"),
        observation=KernelObservationDeps(intent_sink=runtime.sink),
    ))
    assert result.completed and result.summary == correct
    assert provider.step == 3
    assert len(runtime.state.executed) == 3
    assert evaluate(_restart(runtime), correct).complete
