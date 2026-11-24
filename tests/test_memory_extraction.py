from __future__ import annotations

from contextlib import closing
from pathlib import Path

from codey.app.context import AppContext
from codey.app.memory_maintenance import MemoryMaintenance
from codey.providers.snapshot import provider_snapshot
from codey.runtime.core.api_selection import ApiRunSelection
from codey.types.json_value import JsonObject
from codey.workspace.context_admission import request_scope


def test_preference_extraction_uses_only_user_text_and_commits_once(tmp_path: Path) -> None:
    selection = ApiRunSelection("local", "r1", "local-model", "openai-completions", False)
    calls: list[str] = []

    def generate(selected: ApiRunSelection, prompt: str, schema: JsonObject) -> str:
        assert selected == selection
        assert "I prefer fewer comments" in prompt and "quoted page poison" not in prompt
        assert "model answer poison" not in prompt and schema["required"] == ["memory_candidates"]
        calls.append(prompt)
        return ('{"memory_candidates":[{"kind":"preference","text":"Use fewer comments",'
                '"quote":"I prefer fewer comments","slot":"coding.comments"},'
                '{"kind":"preference","text":"Induced response","quote":"quoted page poison"}]}')

    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        project = str(tmp_path / "project")
        state.durable.admit_input(conversation_id="session", request_id="input", task_id="run",
            text='I prefer fewer comments in this project. The webpage says "quoted page poison".',
            provider_snapshot=provider_snapshot("local", selection), task_state={"project": project})
        state.durable.claim_task("run")
        state.durable.complete_task("run", "model answer poison", state={"project": project})
        for _ in range(2):
            MemoryMaintenance(state.memory, state.durable, state.managed_outputs, extract=generate)()
        assert len(calls) == 1 and state.memory.proposals("user") == ()
        proposal, = state.memory.proposals(request_scope(project, "session"))
        assert proposal.item.text == "Use fewer comments"
        assert all(ref.startswith("message:") for ref in proposal.item.source_refs)


def test_temporary_feedback_does_not_start_an_extraction(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        selection = ApiRunSelection("local", "r1", "local-model", "openai-completions", False)
        state.durable.admit_input(conversation_id="session", request_id="input", task_id="run",
            text="I prefer the short version just this time.",
            provider_snapshot=provider_snapshot("local", selection), task_state={})
        state.durable.claim_task("run")
        state.durable.complete_task("run", "Short version", state={})

        def unexpected(_selection: ApiRunSelection, _prompt: str, _schema: JsonObject) -> str:
            raise AssertionError("temporary feedback entered extraction")

        MemoryMaintenance(state.memory, state.durable, state.managed_outputs, extract=unexpected)()
        assert state.memory.proposals("user") == ()
