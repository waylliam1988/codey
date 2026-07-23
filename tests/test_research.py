from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codey import browser_worker, cancellation
from codey.consensus import ConsensusAdvice
from codey.events import RunEvent, run_event_payload
from codey.knowledge import KnowledgeChanges, KnowledgeStore
from codey.models import ToolCall, ToolResult
from codey.research.advisors import EvidencePack, run_research_advisors
from codey.research.browser_search import BrowserSearchProvider
from codey.research.evidence_review import provenance_problem
from codey.research.protocols import JsonToolCodec
from codey.research.runner import ResearchRunner
from codey.research.tools import ResearchTools
from codey.research.url_policy import check_fetch_url


class FakeProvider:
    name = "Fake"
    location = "fake://provider"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0

    def new_chat(self, timeout=None) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        if not self.replies:
            return json.dumps({"tool": "done", "args": {"answer": "done"}})
        return self.replies.pop(0)

    def close(self) -> None:
        pass


class FakeSearch:
    name = "fake-search"

    def search(self, query: str, limit: int = 8) -> list[dict]:
        return [{
            "title": "Helium article",
            "url": "https://example.com/helium",
            "snippet": "Helium supply.",
        }]

    def fetch(self, url: str) -> dict:
        return {
            "url": url,
            "title": "Helium article",
            "text": "Helium is separated from natural gas streams.",
            "truncated": False,
        }

    def close(self) -> None:
        pass


class FakeAdvisorProvider:
    name = "FakeAdvisor"

    def __init__(self, reply: str = "advisor note") -> None:
        self.reply = reply
        self.sent: list[str] = []
        self.new_chat_calls = 0
        self.closed = False

    def new_chat(self) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout=None) -> str:
        del timeout
        self.sent.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


class SlowProvider(FakeProvider):
    thread_safe_send = True

    def __init__(
        self,
        delay: float,
        *,
        started: threading.Event | None = None,
        finished: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.delay = delay
        self.started = started
        self.finished = finished
        self.release = release

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        if self.started is not None:
            self.started.set()
        try:
            if self.release is not None:
                self.release.wait(timeout=5.0)
            else:
                time.sleep(self.delay)
            return json.dumps({"tool": "done", "args": {"answer": "done"}})
        finally:
            if self.finished is not None:
                self.finished.set()


class ResearchBoundaryTests(unittest.TestCase):
    def test_url_policy_rejects_private_targets_without_network(self) -> None:
        self.assertIn("local", check_fetch_url("http://localhost:8000", resolve=False))
        self.assertIn("non-public", check_fetch_url("http://127.0.0.1/", resolve=False))
        self.assertIsNone(check_fetch_url("https://example.com/page", resolve=False))

    def test_browser_worker_exposes_module_call_for_research_search(self) -> None:
        self.assertEqual(browser_worker.call(lambda value: value + 1, 4), 5)

    def test_browser_worker_call_is_reentrant_on_browser_thread(self) -> None:
        def outer() -> str:
            return browser_worker.call(lambda: "nested", timeout=0.5)

        self.assertEqual(browser_worker.call(outer, timeout=1.0), "nested")

    def test_browser_worker_call_observes_task_cancellation_while_waiting(self) -> None:
        event = threading.Event()

        def slow() -> str:
            time.sleep(0.3)
            return "done"

        timer = threading.Timer(0.05, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with cancellation.scope(event):
                with self.assertRaises(cancellation.TaskCancelled):
                    browser_worker.call(slow)
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.25)

    def test_final_summary_requires_exact_opened_url_not_same_domain(self) -> None:
        problem = provenance_problem(
            "来源: https://example.com/b",
            opened_sources={"https://example.com/a"},
            search_result_urls=set(),
        )

        self.assertIn("did not open", problem)

    def test_final_summary_bare_domain_requires_exact_opened_host(self) -> None:
        problem = provenance_problem(
            "来源: example.com",
            opened_sources={"https://foo.example.com/a"},
            search_result_urls=set(),
        )

        self.assertIn("did not open", problem)

    def test_fact_note_requires_opened_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(FakeSearch(), store, KnowledgeChanges(store.root))

            rejected = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })
            tools.sources_read.add("https://example.com/helium")
            accepted = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })
            store.close()

        self.assertTrue(rejected.startswith("ERROR:"))
        self.assertIn("saved fact note", accepted)

    def test_search_result_source_requires_open_without_rendering_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(FakeSearch(), store, KnowledgeChanges(store.root))
            tools.search_result_urls.add("https://example.com/helium")

            result = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })
            count = store.index.count()
            store.close()

        self.assertTrue(result.startswith("NEEDS_OPEN:"))
        self.assertIn("open_url", result)
        self.assertEqual(count, 0)

    def test_needs_open_outcome_is_not_changed_or_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), FakeSearch(), store, max_turns=2)
            runner.tools.search_result_urls.add("https://example.com/helium")
            call = ToolCall("knowledge_write", {
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })

            outcome = runner._dispatch(call)
            payload = run_event_payload(RunEvent.tool_finished(1, call, outcome))
            count = store.index.count()
            store.close()

        self.assertEqual(outcome.status, "needs_action")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)
        self.assertEqual(count, 0)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "needs_action")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["changed"])

    def test_saved_note_outcome_is_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), FakeSearch(), store, max_turns=2)
            runner.tools.sources_read.add("https://example.com/helium")
            call = ToolCall("knowledge_write", {
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })

            outcome = runner._dispatch(call)
            payload = run_event_payload(RunEvent.tool_finished(1, call, outcome))
            count = store.index.count()
            store.close()

        self.assertEqual(outcome.status, "ok")
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.changed)
        self.assertEqual(count, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["changed"])

    def test_research_protocol_guides_open_url_before_note_write(self) -> None:
        codec = JsonToolCodec()
        prompt = codec.system_prompt()
        followup = codec.format_results([
            ToolResult(
                ToolCall("knowledge_write", {"title": "Helium"}),
                "NEEDS_OPEN: open_url before saving this note: https://example.com/helium",
            )
        ])

        self.assertIn("A web_search result is not evidence yet", prompt)
        self.assertIn("call open_url", prompt)
        self.assertIn("NEEDS_OPEN", followup)
        self.assertIn("call open_url", followup)

    def test_research_advisors_get_only_the_evidence_pack(self) -> None:
        advisor = FakeAdvisorProvider("gap found")
        pack = EvidencePack(
            question="Research helium",
            draft="Draft answer",
            opened_urls=("https://example.com/helium",),
            search_result_urls=("https://example.com/search",),
        )

        reports = run_research_advisors(
            selected_provider_id="deepseek",
            provider_ids=("deepseek", "qwen"),
            provider_labels={"deepseek": "DeepSeek", "qwen": "Qwen"},
            availability=lambda: {"qwen": True},
            connect_existing=lambda _provider_id: advisor,
            pack=pack,
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].text, "gap found")
        self.assertEqual(advisor.new_chat_calls, 1)
        self.assertTrue(advisor.closed)
        self.assertIn("Research EvidencePack", advisor.sent[0])
        self.assertIn("https://example.com/helium", advisor.sent[0])
        self.assertIn("https://example.com/search", advisor.sent[0])

    def test_research_tools_default_written_notes_to_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(
                FakeSearch(),
                store,
                KnowledgeChanges(store.root),
                session_id="s1",
                project="E:/project",
            )
            url = "https://example.com/helium"
            tools.sources_read.add(url)

            saved = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": [url],
                "session_id": "attacker",
                "project": "E:/other",
            })
            rows = store.index.recent(5, session_id="s1")
            note = store.read_note(rows[0]["id"])
            store.close()

        self.assertIn("saved fact note", saved)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertEqual(note.session_id, "s1")
        self.assertEqual(note.project, "E:/project")

    def test_runner_writes_synthesis_and_restore_can_revert_run(self) -> None:
        url = "https://example.com/helium"
        provider = FakeProvider(
            json.dumps({"tool": "web_search", "args": {"query": "helium"}}),
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Helium source",
                    "body": "Helium comes from gas processing.",
                    "sources": [url],
                },
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": f"关键证据\n- Helium supply: {url}"},
            }),
        )
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                provider,
                FakeSearch(),
                store,
                session_id="s1",
                max_turns=8,
            )

            events = list(runner.run("Research helium"))
            result = runner.result
            links = store.index.links_for([result.synthesis_id] if result else [])
            restore = runner.changes.restore_result()
            store.rebuild()
            count = store.index.count()
            store.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.synthesis_id)
        self.assertIn(result.synthesis_id, result.notes_created)
        self.assertEqual(result.source_urls, [url])
        self.assertTrue(any(link["kind"] == "derives" for link in links))
        self.assertTrue(any(event.kind == "tool" for event in events))
        self.assertTrue(restore.ok)
        self.assertEqual(count, 0)

    def test_research_provider_send_observes_stop_while_provider_is_blocked(self) -> None:
        stop = threading.Event()
        send_started = threading.Event()
        send_finished = threading.Event()
        release_send = threading.Event()
        runner_returned = threading.Event()
        errors: list[BaseException] = []
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                SlowProvider(0.0, started=send_started, finished=send_finished, release=release_send),
                FakeSearch(),
                store,
                should_stop=stop.is_set,
                max_turns=2,
            )

            def run_research() -> None:
                try:
                    list(runner.run("Research a slow endpoint"))
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    runner_returned.set()

            thread = threading.Thread(target=run_research, name="test-research-runner")
            thread.start()
            self.assertTrue(send_started.wait(1.0))
            stop.set()
            self.assertTrue(runner_returned.wait(1.0))
            self.assertFalse(send_finished.is_set())
            release_send.set()
            self.assertTrue(send_finished.wait(1.0))
            thread.join(timeout=1.0)
            try:
                self.assertFalse(errors)
                self.assertFalse(thread.is_alive())
                self.assertIsNotNone(runner.result)
                assert runner.result is not None
                self.assertEqual(runner.result.stop_reason, "stopped")
            finally:
                store.close()

    def test_web_style_provider_send_stays_on_runner_thread(self) -> None:
        class ThreadRecordingProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__(json.dumps({"tool": "done", "args": {"answer": "done"}}))
                self.thread_ids: list[int] = []

            def send(self, text: str, timeout=None) -> str:
                self.thread_ids.append(threading.get_ident())
                return super().send(text, timeout=timeout)

        provider = ThreadRecordingProvider()
        caller_thread_id = threading.get_ident()
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(provider, FakeSearch(), store, max_turns=2)

            list(runner.run("Research thread model"))
            store.close()

        self.assertEqual(provider.thread_ids, [caller_thread_id])

    def test_research_intro_includes_bounded_chat_handoff(self) -> None:
        provider = FakeProvider(json.dumps({"tool": "done", "args": {"answer": "done"}}))
        handoff = '{"goal":"Compare SQLite and flat files","latest_reply":"Use SQLite."}'
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                provider,
                FakeSearch(),
                store,
                chat_handoff=handoff,
                max_turns=2,
            )

            list(runner.run("Research the prior plan"))
            store.close()

        self.assertIn("Conversation context from this chat", provider.sent[0])
        self.assertIn("Compare SQLite", provider.sent[0])

    def test_runner_uses_private_advisors_before_final_research_answer(self) -> None:
        url = "https://example.com/helium"
        provider = FakeProvider(
            json.dumps({"tool": "web_search", "args": {"query": "helium"}}),
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Helium source",
                    "body": "Helium comes from gas processing.",
                    "sources": [url],
                },
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": "Initial draft without enough evidence."},
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": f"修订版\n- 关键证据\n- {url}"},
            }),
        )
        seen: list[EvidencePack] = []

        def review(pack: EvidencePack):
            seen.append(pack)
            return (ConsensusAdvice("qwen", "Qwen", "Need a direct evidence citation."),)

        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                provider,
                FakeSearch(),
                store,
                session_id="s1",
                max_turns=8,
                review_advisors=review,
            )

            list(runner.run("Research helium"))
            result = runner.result
            store.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.summary, f"修订版\n- 关键证据\n- {url}")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].opened_urls, (url,))
        self.assertEqual(len(seen[0].notes), 1)
        self.assertIn("Need a direct evidence citation.", provider.sent[-1])

    def test_runner_synthesis_records_opened_sources_for_project_brief(self) -> None:
        url = "https://example.com/helium"
        provider = FakeProvider(
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "done",
                "args": {"answer": f"来源\n- Helium supply: {url}"},
            }),
        )
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(provider, FakeSearch(), store, session_id="s1", max_turns=4)

            list(runner.run("Research helium"))
            result = runner.result
            synthesis = store.read_note(result.synthesis_id if result else "")
            store.close()

        self.assertIsNotNone(result)
        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertEqual(synthesis.sources, [url])

    def test_browser_search_fetch_uses_separate_page_from_search_results(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.pages = []

            def new_page(self):
                page = FakePage(self)
                self.pages.append(page)
                return page

        class FakePage:
            def __init__(self, context) -> None:
                self.context = context
                self.routes = []
                self.timeout = None
                self.brought_to_front = False

            def is_closed(self) -> bool:
                return False

            def set_default_navigation_timeout(self, timeout) -> None:
                self.timeout = timeout

            def route(self, pattern, handler) -> None:
                self.routes.append((pattern, handler))

            def bring_to_front(self) -> None:
                self.brought_to_front = True

        context = FakeContext()
        search_page = FakePage(context)
        context.pages.append(search_page)
        session = type("Session", (), {
            "page": search_page,
            "browser": type("Browser", (), {"contexts": [context]})(),
        })()
        provider = BrowserSearchProvider()
        provider._session = session
        provider._search_page = search_page

        fetch_page = provider._ensure_fetch_page_on_browser_thread("https://example.com/article")

        self.assertIsNot(fetch_page, search_page)
        self.assertIn(fetch_page, context.pages)
        self.assertTrue(fetch_page.brought_to_front)


if __name__ == "__main__":
    unittest.main()
