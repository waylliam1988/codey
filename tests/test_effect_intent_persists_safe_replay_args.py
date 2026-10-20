from codey.runtime.core.models import ToolCall, ToolResult


def test_read_intent_persists_canonical_replay_args(tmp_path) -> None:
    from codey.operations.task_effects import KernelEffectSink
    from tests.durable_runtime import accept_writer, open_runtime

    durable, mutations = open_runtime(tmp_path, session_id="s1", run_id="r1")
    accept_writer(durable, mutations, session_id="s1", run_id="r1")
    sink = KernelEffectSink(
        mutations,
        session_id="s1",
        run_id="r1",
        provider_id="local",
        durable=durable,
    )
    sink.begin_turn([("effect-1", ToolCall("read_file", {"path": "a.py"}), 0)], turn=1)

    from codey.runtime.effects.effect_records import RuntimeEffectStore

    effects = RuntimeEffectStore(durable=durable).load_effects("s1", "r1")
    assert effects[0].intent.replay_args == {"path": "a.py"}
    sink.settle("effect-1", True, result=ToolResult(ToolCall("read_file", {"path": "a.py"}), "file contents", ok=True))
    settled = RuntimeEffectStore(durable=durable).load_effects("s1", "r1")[0].settlement
    assert settled is not None
    assert settled.result_excerpt == "file contents"
