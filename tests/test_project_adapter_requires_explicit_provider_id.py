"""Project adapter requires explicit provider_id with durable mutations.

Repro: tests/support/kernel_harness fell back to provider.name when
request.provider_id was empty, masking a missing provider_id. Direct
project_adapter.run() with runtime_mutations and empty provider_id fails
in mutation_line with "provider_id must not be empty".

Lock: empty provider_id with mutations raises explicitly; a matching
explicit provider_id completes.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class ProjectAdapterRequiresExplicitProviderIdTests(unittest.TestCase):
    def test_empty_provider_id_with_mutations_fails_explicitly(self) -> None:
        from codey.agents.request import AgentRequest
        from codey.operations.project_adapter import run as project_run
        from tests.durable_runtime import accept_writer, open_runtime

        class _P:
            name = "mock_provider"

            def send(self, prompt: str, timeout=None) -> str:
                del prompt, timeout
                return '{"tool": "done", "args": {"summary": "ok"}}'

            def new_chat(self) -> None:
                return None

            def close(self) -> None:
                return None

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

    def test_explicit_provider_id_completes(self) -> None:
        from codey.agents.request import AgentRequest
        from tests.support.kernel_runtime import run_agent as project_run

        class _P:
            name = "mock_provider"

            def send(self, prompt: str, timeout=None) -> str:
                del prompt, timeout
                return '{"tool": "done", "args": {"summary": "ok"}}'

            def new_chat(self) -> None:
                return None

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as td:
            result = project_run(
                AgentRequest(
                    provider=_P(),  # type: ignore[arg-type]
                    provider_id="mock_provider",
                    project=Path(td) / "proj",
                    task="hi",
                    session_id="s1",
                    run_id="r1",
                )
            )
            self.assertEqual(result.stop_reason, "done")


if __name__ == "__main__":
    unittest.main()
