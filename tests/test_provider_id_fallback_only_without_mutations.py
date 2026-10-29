"""Missing durable resources or provider identity always fail closed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class _P:
    name = "mock_provider"

    def send(self, prompt: str, timeout=None) -> str:
        del prompt, timeout
        return '{"tool": "done", "args": {"summary": "ok"}}'

    def new_chat(self) -> None:
        return None

    def close(self) -> None:
        return None


class ProviderIdFallbackOnlyWithoutMutationsTests(unittest.TestCase):
    def test_empty_provider_id_with_mutations_raises(self) -> None:
        from codey.agents.request import AgentRequest
        from codey.operations.project_adapter import run as project_run
        from tests.durable_runtime import accept_writer, open_runtime

        with tempfile.TemporaryDirectory() as td:
            durable, line = open_runtime(Path(td) / "state", session_id="s1", run_id="r1", provider_id="mock_provider")
            accept_writer(durable, line, session_id="s1", run_id="r1", project=str(td), provider_id="mock_provider")
            with self.assertRaises(Exception) as cm:
                project_run(
                    AgentRequest(
                        provider=_P(),  # type: ignore[arg-type]
                        provider_id="",
                        project=Path(td) / "proj",
                        task="hi",
                        session_id="s1",
                        run_id="r1",
                        runtime_mutations=line,
                        durable=durable,
                    )
                )
            self.assertIn("provider_id", str(cm.exception))

    def test_empty_provider_id_without_mutations_is_rejected(self) -> None:
        from codey.agents.request import AgentRequest
        from codey.operations.project_adapter import run as project_run

        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            project_run(
            AgentRequest(
                provider=_P(),  # type: ignore[arg-type]
                provider_id="",
                project=Path(td) / "proj",
                task="hi",
                session_id="s1",
                run_id="r1",
            )
            )



if __name__ == "__main__":
    unittest.main()
