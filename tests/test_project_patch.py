import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.agents.request import AgentRequest
from codey.agents.tools import AgentToolFns
from codey.app.headless_runner import HeadlessAppContext
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.project_instructions import INSTRUCTION_CHANGE, changed_instruction_target, instruction_handoff
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import runtime
from codey.toolchain.runtime import ToolOutcome
from codey.types.json_value import JsonObject
from codey.workspace.revision import WorkspaceRevisionStore
from tests.support.kernel_runtime import run_agent


@pytest.mark.parametrize("scenario", ["absent", "syntax", "conflict", "success"])
def test_patch_preflights_all_files_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("first.txt", "second.py"):
        (project / name).write_bytes(b"before\r\nkeep\n")
    if scenario == "conflict":
        final_check = runtime._write_text_after_final_path_check

        def concurrent_write(root: Path, rel: str, content: str, *, tool: str, expected_version: str | None = None) -> ToolOutcome | None:
            if rel == "second.py":
                (root / rel).write_bytes(b"external\n")
            return final_check(root, rel, content, tool=tool, expected_version=expected_version)

        monkeypatch.setattr(runtime, "_write_text_after_final_path_check", concurrent_write)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(project))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path / "state")
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="patch", effect_scope="test",
        turn=1, project_path=project, allow_writes=True, tool_fns=AgentToolFns(), workspace_revision_store=revisions)
    result = execute_turn(session, [ToolCall("codemode", {"inputs": {
        "anchor": "absent" if scenario == "absent" else "before",
        "replacement": "const invalid = 1;" if scenario == "syntax" else "after",
    }, "code": """
        const first = await tools.read_file({path: 'first.txt'});
        await tools.read_file({path: 'second.py'});
        return unwrap(await tools.edit({files: [
            {path: 'first.txt', base_result_ref: first.result_ref, replacements: [{old_string: 'before', new_string: 'after'}]},
            {path: 'second.py', replacements: [{old_string: inputs.anchor, new_string: inputs.replacement}]}
        ]}));
    """})], snapshot=snapshot, run_id="patch", effect_scope="test", turn=1, project_path=project,
        workspace_revision_store=revisions, executors={"codemode": executor})[0]
    if scenario == "success":
        assert result.ok, result.model_text
        expected = b"after\r\nkeep\n"
        assert {row.call.args["path"] for row in session._memory_results.values() if row.call.name == "edit" and row.ok} == {
            "first.txt", "second.py",
        }
    elif scenario == "conflict":
        assert not result.ok and "Patch stopped after writing first.txt" in result.model_text
        assert (project / "first.txt").read_bytes() == b"after\r\nkeep\n"
        assert (project / "second.py").read_bytes() == b"external\n"
        assert session.edited_files.keys() == {"first.txt"}
        return
    else:
        assert not result.ok and "Patch preflight stopped" in result.model_text
        expected = b"before\r\nkeep\n"
        assert all(row.call.name != "edit" for row in session._memory_results.values())
    assert (project / "first.txt").read_bytes() == (project / "second.py").read_bytes() == expected


def test_cross_directory_creation_observes_its_own_instructions_before_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    for folder, rules in (("a", "A_SCOPE: keep existing public API"), ("b", "B_SCOPE: write B files with a sentinel")):
        (project / folder).mkdir(parents=True)
        (project / folder / "AGENTS.md").write_text(rules + "\n", encoding="utf-8")
    (project / "a/reference.py").write_text("LIMIT = 3\n", encoding="utf-8")
    target = project / "b/created.py"
    create: JsonObject = {"tool": "edit", "args": {"path": "b/created.py", "content": "LIMIT = 3\n# sentinel\n"}}
    replies: tuple[JsonObject, ...] = (
        {"tool": "read_file", "args": {"path": "a/reference.py"}},
        create,
        create,
        {"tool": "done", "args": {"summary": "Created the file with the b scope sentinel."}},
    )
    prompts: list[str] = []

    class Provider:
        name = "scripted"
        location = "in-process"

        def new_chat(self, timeout: float | None = None) -> None:
            pass

        def send(self, text: str, timeout: float | None = None) -> str:
            position = len(prompts)
            prompts.append(text)
            if position <= 2:
                assert not target.exists(), "The unfrozen edit wrote a file."
            return json.dumps(replies[min(position, len(replies) - 1)])

        def close(self) -> None:
            pass

    with closing(HeadlessAppContext(tmp_path / "state", port=0, emit_jsonl=lambda _event: None)) as state:
        result = run_agent(AgentRequest(
            Provider(), project, "Create b/created.py using the project instructions. Do not run tests.",
            provider_id="test", durable=state.durable, managed_outputs=state.managed_outputs,
            workspace_revision_store=state.workspace_revisions, runtime_mutations=state.runtime_mutations,
            max_turns=6, project_changes_required=True,
        ))

    assert result.stop_reason == "done", result
    assert "a/AGENTS.md applies under a" in prompts[1] and "B_SCOPE:" not in prompts[1]
    assert "b/AGENTS.md applies under b" in prompts[2] and "B_SCOPE:" in prompts[2]
    assert "a/AGENTS.md applies under a" in prompts[2]
    assert target.read_text(encoding="utf-8") == "LIMIT = 3\n# sentinel\n"


def test_directory_rule_versions_survive_snapshot_restore_and_detect_change_or_deletion(tmp_path: Path) -> None:
    (tmp_path / "package").mkdir()
    rule = tmp_path / "package/AGENTS.md"
    rule.write_text("Use package-specific tests.\n", encoding="utf-8")
    bindings, text = instruction_handoff(tmp_path, ("package/file.py",), {})
    assert "applies under package" in text
    session = TaskSession(TaskPolicy(frozenset({"control", "project.write"})), project=str(tmp_path))
    original = replace(build_turn_snapshot(session), input_contract_version=18, project_instructions=bindings)
    assert restore_snapshot(snapshot_description(original)) == original
    assert changed_instruction_target(tmp_path, "package/file.py", bindings) is None
    assert changed_instruction_target(tmp_path, "another/file.py", bindings) is None
    rule.write_text("Use a different package test.\n", encoding="utf-8")
    assert changed_instruction_target(tmp_path, "package/file.py", bindings) == "package/file.py"
    rule.unlink()
    assert changed_instruction_target(tmp_path, "package/file.py", bindings) == "package/file.py"


def test_full_versioned_rule_pages_unlock_a_truncated_rule(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "package").mkdir(parents=True)
    long_rule = "".join(f"{i:04} " + "instruction text " * 20 + "\n" for i in range(50))
    rule = "package/AGENTS.md"
    (root / rule).write_text(long_rule, encoding="utf-8")
    target = "package/file.py"
    bindings, hint = instruction_handoff(root, (target,), {})
    assert "partial" in hint and changed_instruction_target(root, target, bindings) == target
    receipts: dict[str, ToolResult] = {}
    offset = 1
    while offset:
        page = runtime.read_file(root, rule, offset=offset, limit=10)
        raw = page.canonical["value"]
        assert isinstance(raw, dict) and isinstance(next_offset := raw["next_offset"], int)
        call = ToolCall("read_file", {"path": rule, "offset": offset, "limit": 10})
        receipts[str(offset)] = ToolResult(call, page.model_text, ok=page.ok, truncated=page.truncated,
                                           canonical=page.canonical)
        offset = next_offset
    bindings, hint = instruction_handoff(root, (target,), receipts)
    assert "Complete text was delivered" in hint
    assert changed_instruction_target(root, target, bindings) is None


def test_batch_preflight_rejects_unobserved_destination_before_any_write(tmp_path: Path) -> None:
    for directory in ("a", "b"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "file.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "b/AGENTS.md").write_text("Observe B instructions before writing.", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(session)
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="patch", effect_scope="test",
        turn=1, project_path=tmp_path, allow_writes=True, tool_fns=AgentToolFns())
    call = ToolCall("codemode", {"code": """
        await tools.read_file({path: 'a/file.txt'});
        await tools.read_file({path: 'b/file.txt'});
        const replacements = [{old_string: 'before', new_string: 'after'}];
        await tools.edit({files: [{path: 'a/file.txt', replacements}, {path: 'b/file.txt', replacements}]});
        return 'Guest discarded its edit outcome.';
    """})
    result = execute_turn(session, [call], snapshot=snapshot, run_id="patch", effect_scope="test", turn=1,
                          project_path=tmp_path, executors={"codemode": executor})[0]
    assert result.canonical[INSTRUCTION_CHANGE] == ["b/file.txt"]
    assert all((tmp_path / directory / "file.txt").read_text(encoding="utf-8") == "before\n" for directory in ("a", "b"))


def test_directory_rules_do_not_authorize_a_link_outside_the_project(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "AGENTS.md").write_text("This rule is outside the project.", encoding="utf-8")
    try:
        (project / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory links are unavailable: {exc}")

    bindings, text = instruction_handoff(project, ("linked/file.py",), {})
    assert not bindings and not text
    with pytest.raises(ValueError, match="escapes project root"):
        changed_instruction_target(project, "linked/file.py", bindings)
