from codey.runtime.core.models import ToolCall, ToolResult


def test_read_intent_persists_canonical_replay_args(tmp_path) -> None:
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp_path / "session.log")
    mutations = RuntimeMutationLine(log)
    mutations.accept_operation(
        session_id="s1", run_id="r1", provider_id="local", turn_budget=4,
        max_repair_rounds=0,
    )
    mutations.mark_writer_running("s1", "r1", provider_id="local")
    sink = KernelEffectSink(
        mutations,
        session_id="s1",
        run_id="r1",
        provider_id="local",
    )
    sink.begin_turn([("effect-1", ToolCall("read_file", {"path": "a.py"}), 0)], turn=1)

    from codey.runtime.effects.effect_records import RuntimeEffectStore

    effects = RuntimeEffectStore(log).load_effects("s1", "r1")
    assert effects[0].intent.replay_args == {"path": "a.py"}
    sink.settle("effect-1", True, result=ToolResult(ToolCall("read_file", {"path": "a.py"}), "file contents"))
    settled = RuntimeEffectStore(log).load_effects("s1", "r1")[0].settlement
    assert settled is not None
    assert settled.result_excerpt == "file contents"
