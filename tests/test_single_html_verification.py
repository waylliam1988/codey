from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from codey.agents.request import AgentRequest
from codey.app.context import AppContext
from codey.completion.verification_policy import (
    check_covers_changes,
    check_covers_selected_candidate,
    discover_verification_candidates,
    select_verification_candidate,
)
from codey.operations.task_run import TaskRunDeps
from codey.operations.task_submission import run_task_submission
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.providers.diagnostics import capture_provider_failure
from codey.runtime.core.run_result import RunResult
from codey.task.model import TaskSubmission
from codey.toolchain.runtime import run_command
from codey.types.json_value import JsonObject
from codey.workspace.changes import collect_changes

HTML = '<!doctype html><canvas id="board"></canvas><script>const board = document.querySelector("canvas");</script>'


class HtmlConversation:
    name = "scripted HTML writer"
    location = "test:html-writer"
    native_tools = True

    def __init__(self) -> None:
        self.results: list[ProviderToolResult] = []
        self.turn = 0

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        raise AssertionError("expected native tool calls")

    def _call(self, name: str, args: JsonObject) -> AssistantTurn:
        self.turn += 1
        return AssistantTurn(tool_calls=(ProviderToolCall(str(self.turn), name, args),))

    def send_turn(self, prompt: str, tools: list[ProviderToolDefinition] | None) -> AssistantTurn:
        return self._call("edit", {"path": "snake.html", "content": HTML})

    def send_tool_results(
        self, results: list[ProviderToolResult], tools: list[ProviderToolDefinition] | None,
    ) -> AssistantTurn:
        self.results.extend(results)
        if any("Required verification for the current changes: codey-check-html snake.html" in row.content
               for row in results):
            return self._call("run", {"path": ".", "command": "codey-check-html snake.html"})
        return self._call("done", {"summary": "Created snake.html."})

    def acknowledge_tool_results(
        self, results: list[ProviderToolResult], declared_tools: list[ProviderToolDefinition],
    ) -> AssistantTurn:
        self.results.extend(results)
        return AssistantTurn()


def _project_mode_not_expected(request: AgentRequest) -> RunResult:
    raise AssertionError("the chat workspace must use the shared entry kernel")


def _review_not_expected(**inputs: object) -> None:
    raise AssertionError("the chat workspace must not enter project review")


@pytest.mark.skipif(shutil.which("node") is None, reason="inline JavaScript verification requires Node.js")
def test_native_chat_can_verify_a_new_single_file_html_project() -> None:
    provider = HtmlConversation()
    with TemporaryDirectory(prefix="codey-html-") as directory:
        root = Path(directory)
        workspace = root / "test workspace"
        workspace.mkdir()
        with closing(AppContext(root / "state")) as state:
            deps = TaskRunDeps.from_submission_stores(
                state=state,
                stores=state.task_submission_stores,
                agent_run=_project_mode_not_expected,
                collect_changes=collect_changes,
                run_review=_review_not_expected,
                capture_provider_failure=capture_provider_failure,
            )
            with patch.object(state, "get_provider", return_value=provider):
                run_task_submission(deps, TaskSubmission(
                    "html-chat", str(workspace), "帮我写一个html的贪吃蛇游戏？", 8, False, "deepseek", intent="chat",
                ))
            terminal = state.run_registry.last_terminal_event()
        assert terminal is not None
        assert terminal["stop_reason"] == "done", terminal
        assert list(workspace.iterdir()) == [workspace / "snake.html"]
        assert (workspace / "snake.html").read_text(encoding="utf-8") == HTML
        assert any("exit 0: codey-check-html snake.html" in row.content for row in provider.results)


def test_static_check_is_only_selected_for_the_existing_html_file(tmp_path: Path) -> None:
    path = "public/game board.html"
    file = tmp_path / path
    file.parent.mkdir()
    file.write_text(HTML, encoding="utf-8")
    selected = select_verification_candidate((), (path, "README.md"), root=tmp_path)
    assert selected is not None
    discovered = discover_verification_candidates(tmp_path)
    assert select_verification_candidate(discovered, (path,)) == selected
    assert not discover_verification_candidates(tmp_path, ignored_paths=("public",))
    assert check_covers_changes(selected.command, selected.cwd, (path,), root=tmp_path)
    assert not check_covers_changes(selected.command, selected.cwd, ("public/other.html",), root=tmp_path)
    assert not check_covers_selected_candidate(
        selected, selected.command, selected.cwd, (path, "game.js"), root=tmp_path,
    )
    assert select_verification_candidate((), (path, "game.js"), root=tmp_path) is None
    assert select_verification_candidate((), ("missing.html",), root=tmp_path) is None

    outcome = run_command(tmp_path, ".", "codey-check-html ../outside.html", permission_profile="coding_writer")
    assert not outcome.ok
    assert outcome.exit_code is None


def test_markup_check_is_available_without_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    page = tmp_path / "page.html"
    page.write_text('<!doctype html><p>Example</p><script type="application/ld+json">{}</script>', encoding="utf-8")
    cmd = "codey-check-html page.html"
    markup = run_command(tmp_path, ".", cmd, permission_profile="coding_writer")
    assert markup.ok
    assert markup.exit_code == 0

    page.write_text("<script>let x = 1;", encoding="utf-8")
    invalid_markup = run_command(tmp_path, ".", cmd, permission_profile="coding_writer")
    assert not invalid_markup.ok
    assert invalid_markup.exit_code == 1
    assert "missing its closing tag" in invalid_markup.model_text

    page.write_text("<script>let x = 1;</script>", encoding="utf-8")
    missing_node = run_command(tmp_path, ".", cmd, permission_profile="coding_writer")
    assert not missing_node.ok
    assert missing_node.exit_code == 127
    assert "node: command not found" in missing_node.model_text


@pytest.mark.parametrize("filename", ["@snake.html", " snake.html", "-flag=snake.html", "~snake.html", "贪吃蛇🐍.html"])
def test_html_operands_are_literal_filenames(tmp_path: Path, filename: str) -> None:
    (tmp_path / "snake.html").write_text("", encoding="utf-8")
    (tmp_path / filename).write_text("<p>Literal filename</p>", encoding="utf-8")
    selected = select_verification_candidate((), (filename,), root=tmp_path)
    assert selected is not None
    outcome = run_command(tmp_path, ".", selected.command, permission_profile="coding_writer")
    assert outcome.ok, outcome.model_text
    assert f"{filename} passed basic HTML checks" in outcome.model_text


@pytest.mark.skipif(shutil.which("node") is None, reason="inline JavaScript verification requires Node.js")
@pytest.mark.parametrize(("source", "exit_code"), [
    ("<script>throw new Error('executing project code');</script>", 0),
    ('<script type="module">import "./unresolved.js";</script>', 0),
    ("<script>return 1;</script>", 1),
    ('<script type="module">export const x = ;</script>', 1),
])
def test_html_run_checks_inline_syntax_without_execution(tmp_path: Path, source: str, exit_code: int) -> None:
    (tmp_path / "page.html").write_text(source, encoding="utf-8")
    outcome = run_command(tmp_path, ".", "codey-check-html page.html", permission_profile="coding_writer")
    assert outcome.exit_code == exit_code
    assert outcome.ok == (exit_code == 0)
