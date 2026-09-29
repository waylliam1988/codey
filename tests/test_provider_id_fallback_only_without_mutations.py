"""provider.name fallback is allowed only without durable mutations.

Contract: durable paths require an explicit provider_id; the legacy
provider.name fallback survives only for non-durable (no runtime_mutations)
compat runs where nothing is persisted.

Lock: empty provider_id + mutations raises explicit provider_id error;
empty provider_id without mutations completes via fallback.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class _P:
    name = "mock_provider"

    def send(self, prompt: str, timeout=None) -> str:
        del prompt, timeout
        return '{"tool": "done", "args": {"summary": "ok"}}'

    def close(self) -> None:
        return None


class ProviderIdFallbackOnlyWithoutMutationsTests(unittest.TestCase):
    def test_empty_provider_id_with_mutations_raises(self) -> None:
        from codey.agents.request import AgentRequest
        from codey.operations.project_adapter import run as project_run
        from codey.runtime.log.session_log import RuntimeSessionLog
        from codey.runtime.write.mutation_line import RuntimeMutationLine

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td) / "state")
            line = RuntimeMutationLine(log)
            line.accept_operation(
                session_id="s1", run_id="r1", project=str(td),
                provider_id="mock_provider", turn_budget=5, max_repair_rounds=1, task_kind="project",
            )
            line.mark_writer_running("s1", "r1", provider_id="mock_provider")
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
                    )
                )
            self.assertIn("provider_id", str(cm.exception))

    def test_empty_provider_id_without_mutations_completes_via_fallback(self) -> None:
        from codey.agents.request import AgentRequest
        from codey.operations.project_adapter import run as project_run

        with tempfile.TemporaryDirectory() as td:
            result = project_run(
                AgentRequest(
                    provider=_P(),  # type: ignore[arg-type]
                    provider_id="",
                    project=Path(td) / "proj",
                    task="hi",
                    session_id="s1",
                    run_id="r1",
                )
            )
            self.assertEqual(result.stop_reason, "done")


if __name__ == "__main__":
    unittest.main()
