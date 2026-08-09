import tempfile
import unittest
from unittest import mock

from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.learning_loop import GhostLearningLoop, GhostLearningTurn
from codey.ghost.store import GhostSignalStore


class FakeProvider:
    def __init__(self, reply: str = '{"signals":[]}') -> None:
        self.reply = reply
        self.sent: list[str] = []
        self.new_chat_calls = 0
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sent.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


def _loop(state_home: str) -> GhostLearningLoop:
    return GhostLearningLoop(
        signal_store=GhostSignalStore(state_home),
        inbox_store=GhostInboxStore(state_home),
        hebbian_store=GhostHebbianStore(state_home),
    )


def _turn(
    user_text: str = "以后回答短一点，先给结论",
    *,
    assistant_text: str = "好的。",
    mode: str = "chat",
    project: str = "",
) -> GhostLearningTurn:
    return GhostLearningTurn(
        mode=mode,
        user_text=user_text,
        assistant_text=assistant_text,
        session_id="s1",
        run_id="r1",
        project=project,
        provider_id="fake",
    )


class GhostLearningLoopTests(unittest.TestCase):
    def test_disabled_learning_does_not_call_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            self.assertTrue(inbox.set_learning_enabled(False))
            loop = GhostLearningLoop(
                signal_store=GhostSignalStore(td),
                inbox_store=inbox,
                hebbian_store=GhostHebbianStore(td),
            )
            factory = mock.Mock(return_value=FakeProvider())

            result = loop.learn_from_turn(_turn(), provider_factory=factory)

            self.assertTrue(result.ok)
            self.assertEqual(result.skipped_reason, "learning_disabled")
            factory.assert_not_called()
            self.assertEqual(inbox.list_candidates(), ())

    def test_no_signal_writes_audit_but_not_inbox_or_hebbian(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            provider = FakeProvider('{"signals":[]}')

            result = _loop(td).learn_from_turn(_turn(user_text="继续"), provider_factory=lambda _pid: provider)

            self.assertTrue(result.ok)
            self.assertEqual(result.skipped_reason, "no_signal")
            self.assertTrue(result.signal_audit_written)
            self.assertEqual(len(GhostSignalStore(td).read_all()), 1)
            self.assertEqual(GhostInboxStore(td).list_candidates(), ())
            self.assertEqual(GhostHebbianStore(td).list_nodes(), ())
            self.assertEqual(provider.new_chat_calls, 1)
            self.assertTrue(provider.closed)

    def test_typed_style_signal_reaches_next_turn_directive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            provider = FakeProvider(
                '{"signals":[{'
                '"kind":"style_preference",'
                '"scope":"user",'
                '"summary":"Prefer concise answer-first replies.",'
                '"evidence_quote":"以后回答短一点，先给结论",'
                '"confidence":0.94,'
                '"metadata":{"conflict_key":"reply_length","value_key":"concise"}'
                '}]}'
            )

            result = _loop(td).learn_from_turn(_turn(), provider_factory=lambda _pid: provider)

            self.assertTrue(result.ok)
            self.assertEqual(result.extracted_count, 1)
            self.assertEqual(result.candidates_changed, 1)
            self.assertEqual(result.accepted_count, 1)
            self.assertEqual(result.reinforced_count, 1)
            inbox_rows = GhostInboxStore(td).list_candidates()
            self.assertEqual(inbox_rows[0].status, "accepted")
            directive = build_ghost_directive(GhostHebbianStore(td))
            self.assertIn("reply length = concise", directive.text)
            self.assertNotIn("Prefer concise answer-first replies.", directive.text)

    def test_signal_audit_failure_blocks_inbox_and_hebbian_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            provider = FakeProvider(
                '{"signals":[{'
                '"kind":"style_preference",'
                '"scope":"user",'
                '"summary":"Prefer concise replies.",'
                '"evidence_quote":"以后回答短一点，先给结论",'
                '"confidence":0.94,'
                '"metadata":{"conflict_key":"reply_length","value_key":"concise"}'
                '}]}'
            )
            signal_store = GhostSignalStore(td)
            loop = GhostLearningLoop(
                signal_store=signal_store,
                inbox_store=GhostInboxStore(td),
                hebbian_store=GhostHebbianStore(td),
            )

            with mock.patch.object(signal_store, "append_extraction", return_value=False):
                result = loop.learn_from_turn(_turn(), provider_factory=lambda _pid: provider)

            self.assertFalse(result.ok)
            self.assertEqual(result.skipped_reason, "signal_audit_failed")
            self.assertEqual(GhostInboxStore(td).list_candidates(), ())
            self.assertEqual(GhostHebbianStore(td).list_nodes(), ())

    def test_correction_is_candidate_and_not_reinforced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            provider = FakeProvider(
                '{"signals":[{'
                '"kind":"correction",'
                '"scope":"session",'
                '"summary":"The correct state backend is JSONL.",'
                '"evidence_quote":"正确是 state backend 用 JSONL",'
                '"confidence":0.96'
                '}]}'
            )

            result = _loop(td).learn_from_turn(
                _turn(user_text="正确是 state backend 用 JSONL"),
                provider_factory=lambda _pid: provider,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.accepted_count, 0)
            self.assertEqual(result.reinforced_count, 0)
            self.assertEqual(GhostInboxStore(td).list_candidates()[0].status, "candidate")
            self.assertEqual(GhostHebbianStore(td).list_nodes(), ())

    def test_provider_factory_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = _loop(td).learn_from_turn(
                _turn(),
                provider_factory=mock.Mock(side_effect=RuntimeError("cdp unavailable")),
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.skipped_reason, "learning_error")
            self.assertIn("cdp unavailable", result.diagnostics[0])

    def test_missing_state_stores_are_noop(self) -> None:
        result = GhostLearningLoop(
            signal_store=None,
            inbox_store=None,
            hebbian_store=None,
        ).learn_from_turn(_turn(), provider_factory=mock.Mock())

        self.assertTrue(result.ok)
        self.assertEqual(result.skipped_reason, "ghost_store_disabled")


if __name__ == "__main__":
    unittest.main()
