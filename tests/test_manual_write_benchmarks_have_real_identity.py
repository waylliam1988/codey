"""Write benchmarks must use the same durable identity boundary as production."""
import importlib
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("name", [
    "coding_current_context_ab", "default_verification_ab", "edit_failure_context_ab",
    "impact_guard_ab", "python_syntax_regression_ab", "read_before_edit_ab", "refactor_hint_ab",
])
def test_write_benchmark_entry_binds_real_revision_store(name, monkeypatch, tmp_path):
    module = importlib.import_module("tests.manual." + name)
    provider = SimpleNamespace(id="fake", prompts=[])

    class EntryReached(Exception):
        pass

    def inspect_request(request):
        from codey.workspace.revision import WorkspaceRevisionStore
        assert isinstance(request.workspace_revision_store, WorkspaceRevisionStore)
        before = request.workspace_revision_store.current_state(str(request.project))
        assert before.fingerprint
        (request.project / "identity_probe.py").write_text("x = 2\n", encoding="utf-8")
        after = request.workspace_revision_store.bump_state(str(request.project))
        assert after.revision > before.revision
        assert after.fingerprint != before.fingerprint
        raise EntryReached

    monkeypatch.setattr(module, "_agent_kernel_request", inspect_request)
    with pytest.raises(EntryReached):
        if name in {"coding_current_context_ab", "default_verification_ab"}:
            module._run_arm(provider, module.CASES[0], "baseline", max_turns=4)
        elif name in {"impact_guard_ab", "refactor_hint_ab"}:
            module._run_arm(provider, next(iter(module.CASES.values())), arm="baseline", max_turns=4)
        elif name == "edit_failure_context_ab":
            module._run_arm(provider, "baseline", 4)
        elif name == "python_syntax_regression_ab":
            module._run_arm(provider, arm="baseline", inject_fault=False, max_turns=4)
        else:
            module._write_case(tmp_path, module.CASES[0])
            module._run_case(provider, tmp_path, module.CASES[0], "guard", 4)


def test_context_benchmark_real_edit_verify_done_completes():
    import json

    from tests.manual import coding_current_context_ab as module
    case = module.ContextCase("identity", "Change VALUE to 2 then verify.",
                              {"app.py": "VALUE = 1\n"}, ("app.py",),
                              (module.sys.executable, "-m", "py_compile", "app.py"))
    provider = module._ScriptedProvider(
        json.dumps({"tool": "read_file", "args": {"path": "app.py"}}),
        json.dumps({"tool": "edit", "args": {"path": "app.py", "replacements": [
            {"old_string": "VALUE = 1", "new_string": "VALUE = 2"}]}}),
        json.dumps({"tool": "run", "args": {"command": "python -m py_compile app.py", "path": "."}}),
        json.dumps({"tool": "done", "args": {"summary": "changed and verified"}}),
    )
    row = module._run_arm(provider, case, "context", max_turns=4)
    assert row["stop_reason"] == "done", row


def test_manual_same_run_recovery_smoke_uses_canonical_edit_and_real_identity():
    from tests.manual.safe_tool_replay_smoke import run_same_run_self_test
    assert run_same_run_self_test() is True
