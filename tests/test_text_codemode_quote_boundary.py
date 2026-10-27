"""Recorded missing-escape failure stays rejected; the original loop repairs it."""
import json
from pathlib import Path

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_effects import KernelRecordedProvider
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import TextError, TextRequests, decode_text_turn
from tests.test_required_tool_lineage import _code, _execute, _restart
from tests.test_required_tool_lineage import runtime as runtime

MALFORMED = (Path(__file__).parent / "fixtures/codemode_quoted_request_invalid.txt").read_text(encoding="utf-8").strip()
ARGS = {"query": '"PATTERN.*"', "path": ".", "literal": True, "case_sensitive": True, "include": "*.py"}
BINDING = (require_tool_success("grep", via="codemode", arguments=ARGS,
                               answer_pointer="/text", result_pointer="/matches/0/text"),)


def test_recorded_response_is_invalid_before_any_provider_normalization_or_execution():
    with pytest.raises(json.JSONDecodeError) as error:
        json.loads(MALFORMED)
    assert error.value.pos == 101
    decoded = decode_text_turn(MALFORMED)
    assert isinstance(decoded, TextError) and decoded.kind == "invalid_json"
    assert "JSON-encode the request once" in decoded.message
    assert "before the reported error position" in decoded.message
    assert "Source excerpt" in decoded.message and "No tool in this reply was executed" in decoded.message
    assert len(decoded.message) < 1000


def test_unicode_separators_in_host_results_are_data_in_one_worker_frame():
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import ToolCatalogEntry

    value = "中文\u2028line\u2029paragraph\\\""
    result = NodeWorkerRuntime().execute("return (await tools.echo({})).text;",
        catalog=(ToolCatalogEntry("echo", "echo", {"type": "object"}),),
        invoke=lambda _name, _args: {"text": value})
    assert result.ok and result.value == value


@pytest.mark.parametrize("runtime", [None], indirect=True)
@pytest.mark.parametrize("value", ['"quoted text"', "single ' and double \" quotes", r'C:\new\tab\"name"',
                                    "换行\n制表\t斜杠\\引号\"", "\\" * 12 + '"', "\u2028\u2029"])
def test_valid_nested_json_preserves_every_character_through_decoder_and_worker(runtime, value):
    code = "return " + json.dumps(value, ensure_ascii=False) + ";"
    item = _code(code)
    decoded = decode_text_turn(json.dumps(item, ensure_ascii=False))
    assert isinstance(decoded, TextRequests) and decoded.items[0].args["code"] == code
    result, = _execute(runtime, [item], False)
    assert result.ok and result.canonical["value"] == value
    assert json.loads(result.model_text)["value"] == value


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
def test_original_loop_repairs_recorded_escape_failure_then_searches_exact_quoted_text_once(runtime):
    (runtime.project / "sample.py").write_text('"PATTERN.*"\n', encoding="utf-8")
    correct = json.dumps({"text": '"PATTERN.*"'})
    snapshot = build_turn_snapshot(runtime.state, native=False)
    codemode = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    examples = [json.loads(example) for example in codemode.json_examples]
    assert examples and all(example["tool"] == "codemode" for example in examples)

    class Provider:
        step = 0

        def send(self, prompt, timeout=None):
            self.step += 1
            if self.step == 1:
                return MALFORMED
            if self.step == 2:
                assert runtime.state.executed == {} and runtime.state._memory_results == {}
                assert "Preserve argument strings exactly" in prompt
                assert "codemode(args:" in prompt and '"inputs"?' in prompt
                assert "No tool in this reply was executed" in prompt
                code = ('const r = await tools.grep({query:\'"PATTERN.*"\',path:\'.\',literal:true,'
                        "case_sensitive:true,include:'*.py'}); if (!r.ok) throw new Error(r.error); "
                        "return r.value.matches[0].text;")
                return json.dumps(_code(code))
            assert self.step == 3 and len(runtime.state.executed) == 2
            return json.dumps({"tool": "done", "args": {"summary": correct}})

    provider = Provider()
    result = run_task_kernel(runtime.state, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=KernelRecordedProvider(provider, runtime.sink), run_id="r",
            provider_id="local", effect_scope="task", user_task="Search exact quoted text and return JSON."),
        execution=KernelExecutionDeps(project_path=runtime.project, managed_outputs=runtime.managed, session_id="s",
            durable=runtime.mutations.durable, workspace_revision_store=runtime.revisions,
            permission_profile="planning_readonly"),
        observation=KernelObservationDeps(intent_sink=runtime.sink),
    ))
    assert result.completed and result.summary == correct and provider.step == 3
    leaf = next(row for row in runtime.state._memory_results.values() if row.call.name == "grep")
    assert leaf.call.args == ARGS and leaf.canonical["value"]["matches"][0]["text"] == '"PATTERN.*"'
    restored = _restart(runtime)
    assert len(restored.executed) == 2
