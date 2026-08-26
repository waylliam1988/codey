from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from codey.ghost.extractor import GhostSignalExtractor
from codey.ghost.schema import (
    MAX_SIGNALS_PER_TURN,
    SENSITIVE_SIGNAL_DIAGNOSTIC,
    GhostSignalParseResult,
    quote_is_grounded,
)
from codey.ghost.signal_codec import GhostSignalCodec
from codey.ghost.store import GhostSignalStore
from codey.server import State
from tests.manual import ghost_signal_extractor_ab as ghost_ab
from tests.manual.ghost_signal_extractor_ab import _should_close_provider


ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    name = "fake"
    location = "fake://ghost"

    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.sent: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        return None

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sent.append(text)
        if self.error is not None:
            raise self.error
        return self.reply

    def close(self) -> None:
        return None


def _reply(*signals: dict[str, object]) -> str:
    return json.dumps({"signals": list(signals)}, ensure_ascii=False)


class GhostSignalCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.codec = GhostSignalCodec()

    def test_parse_style_preference_with_grounded_quote(self) -> None:
        user_text = "以后请先给结论，然后再解释原因。"
        result = self.codec.parse(
            _reply({
                "kind": "style_preference",
                "scope": "user",
                "summary": "Prefer answers that start with the conclusion.",
                "evidence_quote": "以后请先给结论",
                "confidence": 0.94,
                "metadata": {"conflict_key": "reply_structure", "value_key": "answer_first"},
            }),
            user_text=user_text,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].kind, "style_preference")
        self.assertEqual(result.signals[0].metadata["conflict_key"], "reply_structure")
        self.assertEqual(result.signals[0].metadata["value_key"], "answer_first")
        self.assertTrue(quote_is_grounded(result.signals[0].evidence_quote, user_text))

    def test_parse_correction_signal(self) -> None:
        user_text = "你刚才说错了，正确是这个项目不应该直接搬某个 torch 模块。"
        result = self.codec.parse(
            _reply({
                "kind": "correction",
                "scope": "session",
                "summary": "This project should not directly port the torch module.",
                "evidence_quote": "正确是这个项目不应该直接搬某个 torch 模块",
                "confidence": 0.9,
            }),
            user_text=user_text,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.signals[0].kind, "correction")
        self.assertEqual(result.signals[0].scope, "session")

    def test_no_signal_is_valid_empty_result(self) -> None:
        result = self.codec.parse('{"signals":[]}', user_text="继续")

        self.assertTrue(result.ok)
        self.assertFalse(result.signals)
        self.assertFalse(result.diagnostics)

    def test_rejects_ungrounded_evidence_quote(self) -> None:
        result = self.codec.parse(
            _reply({
                "kind": "research_interest",
                "scope": "user",
                "summary": "Track helium and copper links.",
                "evidence_quote": "以后永远研究铜和氦气",
                "confidence": 0.8,
            }),
            user_text="这个研究方向很重要。",
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.signals)
        self.assertIn("evidence_quote not grounded", result.diagnostics[0])

    def test_rejects_unknown_kind(self) -> None:
        result = self.codec.parse(
            _reply({
                "kind": "personality",
                "scope": "user",
                "summary": "Be friendly.",
                "evidence_quote": "以后友好一点",
                "confidence": 0.7,
            }),
            user_text="以后友好一点",
        )

        self.assertFalse(result.ok)
        self.assertIn("unknown kind", result.diagnostics[0])

    def test_rejects_secret_like_signal_text(self) -> None:
        user_text = "以后记住我的 API key 是 sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456。"
        result = self.codec.parse(
            _reply({
                "kind": "correction",
                "scope": "user",
                "summary": "Remember the user's API key.",
                "evidence_quote": "API key 是 sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                "confidence": 0.91,
            }),
            user_text=user_text,
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.signals)
        self.assertTrue(any(SENSITIVE_SIGNAL_DIAGNOSTIC in item for item in result.diagnostics))

    def test_rejects_high_entropy_signal_text(self) -> None:
        secret = "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29"
        user_text = f"以后记住这个值 {secret}。"
        result = self.codec.parse(
            _reply({
                "kind": "long_term_goal",
                "scope": "user",
                "summary": f"Remember {secret}.",
                "evidence_quote": secret,
                "confidence": 0.91,
            }),
            user_text=user_text,
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.signals)
        self.assertTrue(any(SENSITIVE_SIGNAL_DIAGNOSTIC in item for item in result.diagnostics))

    def test_path_like_signal_text_is_allowed(self) -> None:
        # Ordinary source paths are not secrets: the shared high-entropy
        # exemption must hold on the Ghost boundary too.
        user_text = "以后重构 src/main/java/util/ArrayList.java 这个文件。"
        result = self.codec.parse(
            _reply({
                "kind": "correction",
                "scope": "session",
                "summary": "Refactor src/main/java/util/ArrayList.java.",
                "evidence_quote": "src/main/java/util/ArrayList.java",
                "confidence": 0.9,
            }),
            user_text=user_text,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.signals), 1)

    def test_rejects_provider_secret_shapes_in_signal_text(self) -> None:
        secrets = (
            "AKIAIOSFODNN7EXAMPLE",
            "github_pat_AAAA0123456789bbbbbbbbbbbbCCCCCCCC",
            "sk" + "_live_" + "abcdefghijklmnop1234567890",
        )
        for secret in secrets:
            with self.subTest(secret=secret):
                user_text = f"以后把 {secret} 放进配置。"
                result = self.codec.parse(
                    _reply({
                        "kind": "correction",
                        "scope": "session",
                        "summary": f"Store {secret} in config.",
                        "evidence_quote": secret,
                        "confidence": 0.91,
                    }),
                    user_text=user_text,
                )

                self.assertFalse(result.ok)
                self.assertFalse(result.signals)
                self.assertTrue(any(SENSITIVE_SIGNAL_DIAGNOSTIC in item for item in result.diagnostics))

    def test_rejects_multiple_json_objects(self) -> None:
        result = self.codec.parse('{"signals":[]}\n{"signals":[]}', user_text="继续")

        self.assertFalse(result.ok)
        self.assertIn("too_many_json", result.diagnostics[0])

    def test_caps_signal_count(self) -> None:
        user_text = "以后请先给结论。不要写营销味。我更喜欢短一点。"
        signals = [
            {
                "kind": "style_preference",
                "scope": "user",
                "summary": f"preference {index}",
                "evidence_quote": "以后请先给结论",
                "confidence": 0.8,
            }
            for index in range(MAX_SIGNALS_PER_TURN + 2)
        ]
        result = self.codec.parse(json.dumps({"signals": signals}, ensure_ascii=False), user_text=user_text)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.signals), MAX_SIGNALS_PER_TURN)
        self.assertTrue(any("too_many_signals" in item for item in result.diagnostics))

    def test_format_request_marks_assistant_context_as_non_evidence(self) -> None:
        prompt = self.codec.format_request(
            user_text="以后请先给结论",
            assistant_text="The assistant previously said something.",
        )

        self.assertIn("User message:", prompt)
        self.assertIn("do not quote this as evidence", prompt)
        self.assertIn("以后请先给结论", prompt)
        self.assertIn("Allowed metadata conflict_key/value_key pairs", prompt)
        self.assertIn("reply_length=concise", prompt)

    def test_model_visible_system_prompt_does_not_expose_internal_product_names(self) -> None:
        prompt = self.codec.system_prompt()

        self.assertNotIn("Codey", prompt)
        self.assertNotIn("Ghost", prompt)


class GhostSignalExtractorTests(unittest.TestCase):
    def test_extract_uses_provider_and_parses_reply(self) -> None:
        provider = FakeProvider(_reply({
            "kind": "action_tendency",
            "scope": "user",
            "summary": "Verify evidence before answering similar questions.",
            "evidence_quote": "这种问题以后先查证据再回答",
            "confidence": 0.88,
        }))
        extractor = GhostSignalExtractor()

        result = extractor.extract(
            provider=provider,
            user_text="这种问题以后先查证据再回答",
            provider_id="fake",
            timeout=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider_id, "fake")
        self.assertEqual(result.signals[0].kind, "action_tendency")
        self.assertEqual(len(provider.sent), 1)

    def test_provider_failure_is_fail_open_no_signal(self) -> None:
        extractor = GhostSignalExtractor()

        result = extractor.extract(
            provider=FakeProvider(error=RuntimeError("provider stalled")),
            user_text="以后请先给结论",
            provider_id="fake",
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.signals)
        self.assertIn("provider_error", result.diagnostics[0])


class GhostSignalStoreTests(unittest.TestCase):
    def test_store_appends_candidates_without_full_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GhostSignalStore(tmp)
            result = GhostSignalParseResult(
                signals=GhostSignalCodec().parse(
                    _reply({
                        "kind": "long_term_goal",
                        "scope": "user",
                        "summary": "Make the continuity layer central to the system.",
                        "evidence_quote": "我想要把长期连续性层变成这个系统的核心",
                        "confidence": 0.93,
                    }),
                    user_text="我想要把长期连续性层变成这个系统的核心。这里还有一大段普通聊天正文。",
                    provider_id="fake",
                ).signals,
                ok=True,
                provider_id="fake",
            )

            self.assertTrue(store.append_extraction(result, session_id="s1", run_id="r1", project="E:/codey"))
            rows = store.read_recent(1)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["type"], "ghost_signal_extraction")
            raw = store.path.read_text(encoding="utf-8")
            self.assertIn("Make the continuity layer central", raw)
            self.assertNotIn("这里还有一大段普通聊天正文", raw)

    def test_read_recent_zero_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GhostSignalStore(tmp)
            store.append_extraction(GhostSignalParseResult(ok=True))

            self.assertEqual(store.read_recent(0), ())

    def test_delete_all_removes_signal_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GhostSignalStore(tmp)
            store.append_extraction(GhostSignalParseResult(ok=True))

            store.delete_all()

            self.assertFalse(store.path.exists())

    def test_bare_state_disables_ghost_signal_store(self) -> None:
        self.assertIsNone(State().ghost_signals)


class GhostArchitectureTests(unittest.TestCase):
    def test_ghost_package_does_not_import_torch_or_transformers(self) -> None:
        forbidden = {"torch", "transformers"}
        for path in (ROOT / "codey" / "ghost").glob("*.py"):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                imports: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module.split(".")[0])
                self.assertFalse(imports & forbidden)

    def test_importing_ghost_store_does_not_load_provider_browser_stack(self) -> None:
        script = (
            "import json, sys\n"
            "import codey.ghost.store\n"
            "print(json.dumps({"
            "'browser': 'codey.browser' in sys.modules, "
            "'registry': 'codey.providers.registry' in sys.modules, "
            "'deepseek': 'codey.providers.web_drivers.deepseek' in sys.modules, "
            "'qwen': 'codey.providers.web_drivers.qwen' in sys.modules"
            "}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=True,
        )
        loaded = json.loads(completed.stdout)

        self.assertFalse(loaded["browser"])
        self.assertFalse(loaded["registry"])
        self.assertFalse(loaded["deepseek"])
        self.assertFalse(loaded["qwen"])

    def test_manual_keep_open_still_closes_non_isolated_automation(self) -> None:
        self.assertTrue(_should_close_provider(keep_open=True, isolated=False))
        self.assertFalse(_should_close_provider(keep_open=True, isolated=True))
        self.assertTrue(_should_close_provider(keep_open=False, isolated=False))

    def test_manual_ab_writes_failure_payload_when_connect_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "ghost-ab.json"
            with unittest.mock.patch.object(
                ghost_ab,
                "_connect_live_provider",
                side_effect=RuntimeError("cdp attach failed"),
            ):
                code = ghost_ab.main([
                    "--provider",
                    "deepseek",
                    "--output",
                    str(output),
                ])

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("cdp attach failed", payload["error"])
        self.assertEqual(payload["rows"][0]["case"], "connect_or_run")


if __name__ == "__main__":
    unittest.main()
