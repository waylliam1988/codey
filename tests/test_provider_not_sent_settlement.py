"""Provider 发送失败的 sent_state：prep/overflow 为 NOT_SENT，其余为 MAYBE_SENT。

NOT_SENT 证明什么都没发出去，同一 delivery batch 可安全重试；
MAYBE_SENT 则不可盲重试。KernelRecordedProvider 与旧
prompt_context._fail_provider_send 保持同一判定。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.storage.managed_outputs import ManagedOutputStore


class ProviderNotSentSettlementTests(unittest.TestCase):
    def test_prep_overflow_and_known_transport_refusal_settle_not_sent(self) -> None:
        from codey.operations.task_effects import KernelEffectSink
        from codey.providers import error_classification as errors
        from codey.providers.api_transport import GenerationNotSentError
        from codey.runtime.effects.effect_records import SENT_STATE_NOT_SENT, RuntimeEffectStore
        from tests.durable_runtime import accept_writer, open_runtime
        from tests.support.provider_actions import ProviderActions

        with tempfile.TemporaryDirectory() as td:
            durable, line = open_runtime(Path(td) / "state", session_id="s1", run_id="r1")
            accept_writer(durable, line, session_id="s1", run_id="r1", project=str(td))
            for exc in (errors.RequestPrepError("prep boom"), errors.ContextOverflowError("full"), GenerationNotSentError("connection refused")):
                sink = KernelEffectSink(line, session_id="s1", run_id="r1", provider_id="local", durable=durable, managed_outputs=ManagedOutputStore(Path(td)))

                class BoomProvider:
                    def __init__(self, failure: Exception) -> None:
                        self._failure = failure

                    def send(self, prompt, timeout=None):
                        raise self._failure

                BoomProvider.__name__ = f"BoomProvider_{type(exc).__name__}"

                recorded = ProviderActions(BoomProvider(exc), sink)
                with self.assertRaises((errors.RequestPrepError, errors.ContextOverflowError, GenerationNotSentError)):
                    recorded.send("hello")
                store = RuntimeEffectStore(durable=durable)
                sends = [r for r in store.load_effects("s1", "r1") if r.intent.effect_category == "provider_send"]
                self.assertTrue(sends, "provider send intent must be recorded")
                last = sends[-1]
                self.assertIsNotNone(last.settlement)
                assert last.settlement is not None
                self.assertEqual(
                    last.settlement.sent_state, SENT_STATE_NOT_SENT,
                    f"{type(exc).__name__} must settle NOT_SENT for safe retry, got {last.settlement.sent_state}",
                )

    def test_generic_failure_settles_maybe_sent(self) -> None:
        from codey.operations.task_effects import KernelEffectSink
        from codey.runtime.effects.effect_records import SENT_STATE_MAYBE_SENT, RuntimeEffectStore
        from tests.durable_runtime import accept_writer, open_runtime
        from tests.support.provider_actions import ProviderActions

        with tempfile.TemporaryDirectory() as td:
            durable, line = open_runtime(Path(td) / "state", session_id="s2", run_id="r2")
            accept_writer(durable, line, session_id="s2", run_id="r2", project=str(td))
            sink = KernelEffectSink(line, session_id="s2", run_id="r2", provider_id="local", durable=durable, managed_outputs=ManagedOutputStore(Path(td)))

            class BoomProvider:
                def send(self, prompt, timeout=None):
                    raise RuntimeError("net down")

            recorded = ProviderActions(BoomProvider(), sink)
            with self.assertRaises(RuntimeError):
                recorded.send("hello")
            store = RuntimeEffectStore(durable=durable)
            sends = [r for r in store.load_effects("s2", "r2") if r.intent.effect_category == "provider_send"]
            self.assertEqual(sends[-1].settlement.sent_state, SENT_STATE_MAYBE_SENT)


if __name__ == "__main__":
    unittest.main()
