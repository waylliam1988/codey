from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_agent_runtime_has_no_browser_or_deepseek_dependency(self) -> None:
        imports = imported_modules(ROOT / "codey" / "agent.py")

        self.assertNotIn("playwright.sync_api", imports)
        self.assertNotIn("codey.browser", imports)
        self.assertNotIn("codey.deepseek", imports)
        self.assertNotIn("codey.qwen", imports)
        self.assertNotIn("codey.stepfun", imports)
        self.assertNotIn("codey.glm", imports)
        self.assertIn("codey.providers", imports)
        self.assertIn("codey.protocols", imports)

    def test_orchestrators_create_providers_instead_of_browser_sessions(self) -> None:
        for name in ("cli.py", "server.py", "task_runner.py"):
            with self.subTest(name=name):
                imports = imported_modules(ROOT / "codey" / name)
                self.assertNotIn("codey.browser", imports)
                self.assertNotIn("codey.deepseek", imports)
                self.assertNotIn("codey.qwen", imports)
                self.assertNotIn("codey.stepfun", imports)
                self.assertNotIn("codey.glm", imports)

    def test_http_server_delegates_task_orchestration(self) -> None:
        imports = imported_modules(ROOT / "codey" / "server.py")
        source = (ROOT / "codey" / "server.py").read_text(encoding="utf-8")

        self.assertIn("codey.task_runner", imports)
        self.assertNotIn("on_shell_request(cwd_rel", source)
        self.assertNotIn("conversation.prepare_model_handoff", source)

    def test_task_runner_has_no_http_dependency(self) -> None:
        imports = imported_modules(ROOT / "codey" / "task_runner.py")

        self.assertNotIn("http.server", imports)
        self.assertNotIn("codey.server", imports)

    def test_research_pipeline_owns_iteration_boundary_without_legacy_seams(self) -> None:
        pipeline = ROOT / "codey" / "research" / "pipeline.py"
        task_runner = ROOT / "codey" / "task_runner.py"
        research_runner = ROOT / "codey" / "research" / "runner.py"
        pipeline_source = pipeline.read_text(encoding="utf-8")
        task_runner_source = task_runner.read_text(encoding="utf-8")
        research_runner_source = research_runner.read_text(encoding="utf-8")

        self.assertIn("ResearchIterationRun", pipeline_source)
        self.assertIn("ResearchIterationRun", task_runner_source)
        self.assertNotIn("codey.task_runner", imported_modules(pipeline))
        self.assertNotIn("codey.server", imported_modules(pipeline))
        self.assertNotIn("_run_research_task", task_runner_source)
        self.assertNotIn("close_search", pipeline_source)
        self.assertNotIn("runtime_tools", research_runner_source)

    def test_ghost_runtime_has_no_provider_browser_tool_or_research_dependency(self) -> None:
        forbidden = {
            "torch",
            "transformers",
            "codey.browser",
            "codey.providers",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.research.tools",
        }
        for path in (ROOT / "codey" / "ghost").glob("*.py"):
            with self.subTest(path=path.name):
                imports = imported_modules(path)
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_affinity_boundaries_do_not_expand_execution_or_research_authority(self) -> None:
        affinity_imports = imported_modules(ROOT / "codey" / "ghost" / "affinity.py")
        forbidden_affinity = {
            "torch",
            "transformers",
            "codey.browser",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.research.tools",
        }
        self.assertTrue(
            forbidden_affinity.isdisjoint(affinity_imports),
            sorted(forbidden_affinity & affinity_imports),
        )

        research_imports = imported_modules(ROOT / "codey" / "research" / "runner.py")
        permission_imports = imported_modules(ROOT / "codey" / "permission_profiles.py")
        repair_source = (ROOT / "codey" / "adapter_repair.py").read_text(encoding="utf-8")
        tool_runtime_imports = imported_modules(ROOT / "codey" / "tool_runtime.py")

        self.assertNotIn("codey.ghost.affinity", research_imports)
        self.assertNotIn("codey.ghost.affinity", permission_imports)
        self.assertNotIn("affinity", repair_source.casefold())
        self.assertNotIn("codey.ghost", tool_runtime_imports)

    def test_context_epoch_is_projection_only_leaf(self) -> None:
        # Context Epoch projects admission metadata over already-rendered
        # sources; it must stay a stdlib-only leaf with no runtime imports
        # and no I/O of its own.
        path = ROOT / "codey" / "context_epoch.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        internal_imports = sorted(
            name for name in imports if name == "codey" or name.startswith("codey.")
        )
        self.assertEqual(internal_imports, [])
        forbidden_source = (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
            "pathlib",
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_prompt_envelope_is_not_a_provider_or_tool_runtime_seam(self) -> None:
        imports = imported_modules(ROOT / "codey" / "prompt_envelope.py")
        forbidden = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.ghost",
        }

        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_capability_registry_is_metadata_only(self) -> None:
        path = ROOT / "codey" / "capabilities.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.server",
            "codey.task_runner",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "execute(",
            "eval(",
            "exec(",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_task_runner_does_not_use_capability_registry_for_decisions(self) -> None:
        source = (ROOT / "codey" / "task_runner.py").read_text(encoding="utf-8")

        self.assertIn("self.capabilities = capabilities", source)
        self.assertEqual(source.count("self.capabilities"), 1)
        self.assertNotIn("if capabilities", source)
        self.assertNotIn("if self.capabilities", source)

    def test_builtin_profiles_are_metadata_only(self) -> None:
        path = ROOT / "codey" / "builtin_profiles.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.research.runner",
            "codey.server",
            "codey.task_runner",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "execute(",
            "eval(",
            "exec(",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_task_runner_does_not_use_builtin_profiles_for_decisions(self) -> None:
        source = (ROOT / "codey" / "task_runner.py").read_text(encoding="utf-8")

        self.assertIn("self.builtin_profiles = builtin_profiles", source)
        self.assertEqual(source.count("self.builtin_profiles"), 1)
        self.assertNotIn("if builtin_profiles", source)
        self.assertNotIn("if self.builtin_profiles", source)

    def test_action_policy_is_not_runtime_or_plugin_host(self) -> None:
        path = ROOT / "codey" / "action_policy.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "subprocess.",
            "eval(",
            "exec(",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_run_details_is_read_only_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "run_details.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "subprocess.",
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_research_object_model_is_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "research" / "object_model.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
            "codey.ghost",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "entry_points",
            "load_plugin",
            "register_runtime",
            "dispatch(",
            "subprocess.",
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_research_identity_ledger_and_proof_do_not_import_runtime_layers(self) -> None:
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.server",
            "codey.task_runner",
            "codey.ghost",
            "importlib",
            "pkgutil",
            "subprocess",
        }
        for path in (
            ROOT / "codey" / "research" / "identity.py",
            ROOT / "codey" / "research" / "evidence_ledger.py",
            ROOT / "codey" / "research" / "proof_quality.py",
            ROOT / "codey" / "research" / "completion_gate.py",
            ROOT / "codey" / "research" / "source_connectors.py",
            ROOT / "codey" / "research" / "connector_domains.py",
            ROOT / "codey" / "research" / "query_planner.py",
            ROOT / "codey" / "research" / "connector_search.py",
            ROOT / "codey" / "research" / "redaction.py",
            ROOT / "codey" / "research" / "plan_executor.py",
            ROOT / "codey" / "research" / "evidence_followup.py",
            ROOT / "codey" / "research" / "record_merge.py",
        ):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )

    def test_research_review_and_local_context_do_not_import_tool_runtime(self) -> None:
        paths = [
            *(ROOT / "codey" / "research").glob("*.py"),
            ROOT / "codey" / "review.py",
            ROOT / "codey" / "review_coordinator.py",
            *(ROOT / "codey" / "ghost").glob("*.py"),
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertNotIn("codey.tool_runtime", imports)
                # managed_outputs sits on the runtime side of the boundary
                # (it imports tool_runtime), so research/review/ghost modules
                # must consume normalized metadata dicts instead.
                self.assertNotIn("codey.managed_outputs", imports)

    def test_analysis_run_projection_stays_pure(self) -> None:
        analysis_run_imports = imported_modules(ROOT / "codey" / "research" / "analysis_run.py")
        lineage_imports = imported_modules(ROOT / "codey" / "research" / "artifact_lineage.py")
        capsule_imports = imported_modules(ROOT / "codey" / "research" / "reproducibility.py")
        forbidden = {
            "codey.events",
            "codey.tool_runtime",
            "codey.managed_outputs",
            "codey.task_runner",
            "codey.server",
        }
        for name, imports in (
            ("analysis_run", analysis_run_imports),
            ("artifact_lineage", lineage_imports),
            ("reproducibility", capsule_imports),
        ):
            with self.subTest(module=name):
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_completion_contract_modules_are_projection_only(self) -> None:
        # The completion contract is the Verified Completion Gate's pure core:
        # it derives proofs from facts handed to it, and must never reach
        # execution layers, providers, I/O, or model-visible surfaces. The
        # queue gate itself stays under the existing research import-boundary
        # test above.
        paths = (
            ROOT / "codey" / "completion_contract.py",
            ROOT / "codey" / "research" / "contract.py",
        )
        forbidden_imports = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.managed_outputs",
            "codey.events",
            "codey.server",
            "codey.task_runner",
            "codey.ghost",
            "codey.review",
            "importlib",
            "pkgutil",
            "subprocess",
            "urllib",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
            "open(",
            "pathlib",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )
                source = path.read_text(encoding="utf-8")
                for token in forbidden_source:
                    self.assertNotIn(token, source)

    def test_evidence_runtime_and_review_finding_are_projection_only(self) -> None:
        # Evidence Runtime and ReviewFinding explain facts that already exist;
        # they must not reach execution layers, providers, the A/B journal, or
        # the code review parser they intentionally do not migrate.
        paths = (
            ROOT / "codey" / "research" / "evidence_runtime.py",
            ROOT / "codey" / "research" / "review_finding.py",
        )
        forbidden = {
            "codey.browser",
            "codey.deepseek",
            "codey.qwen",
            "codey.stepfun",
            "codey.glm",
            "codey.providers",
            "codey.provider_controls",
            "codey.tool_runtime",
            "codey.managed_outputs",
            "codey.server",
            "codey.task_runner",
            "codey.ghost",
            "codey.review",
            "ab_journal",
            "tests.manual.ab_journal",
            "importlib",
            "pkgutil",
            "subprocess",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))
                source = path.read_text(encoding="utf-8")
                for token in forbidden_source:
                    self.assertNotIn(token, source)

    def test_ab_journal_is_manual_layer_only(self) -> None:
        # The A/B journal is manual-experiment tooling: production layers must
        # not consume it, and it must not depend on production orchestration.
        journal_path = ROOT / "tests" / "manual" / "ab_journal.py"
        journal_imports = imported_modules(journal_path)
        self.assertTrue(
            {"codey.run_trace", "codey.research.evidence_ledger", "codey.task_runner", "codey.server"}.isdisjoint(
                journal_imports
            ),
            sorted(journal_imports),
        )

        consumers = [
            ROOT / "codey" / "run_trace.py",
            *(ROOT / "codey" / "research").glob("*.py"),
            ROOT / "codey" / "task_runner.py",
            ROOT / "codey" / "server.py",
        ]
        for path in consumers:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertNotIn("ab_journal", imports)
                self.assertNotIn("tests.manual.ab_journal", imports)

    def test_transcript_archive_cannot_become_evidence(self) -> None:
        # Transcript replay material stays in the manual layer; the evidence
        # ledger and object model must not know transcripts exist.
        for name in ("evidence_ledger.py", "object_model.py"):
            imports = imported_modules(ROOT / "codey" / "research" / name)
            with self.subTest(module=name):
                self.assertNotIn("ab_journal", imports)
                self.assertNotIn("tests.manual.ab_journal", imports)

    def test_stamped_capability_ids_are_registered_boundaries(self) -> None:
        # Every capability_id literal stamped onto a prompt section or context
        # source in production code must name a registered capability.
        from codey.capabilities import builtin_capability_registry

        registered = set(builtin_capability_registry().ids())
        stamped: dict[str, set[str]] = {}

        for path in sorted((ROOT / "codey").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "capability_id":
                        continue
                    value = keyword.value
                    if (
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value
                    ):
                        stamped.setdefault(
                            path.relative_to(ROOT).as_posix(), set()
                        ).add(value.value)

        self.assertTrue(stamped, "expected capability_id stamps in production code")
        offenders = {
            path: sorted(ids - registered)
            for path, ids in stamped.items()
            if ids - registered
        }
        self.assertEqual(offenders, {})

    def test_refactor_has_no_test_only_compatibility_residue(self) -> None:
        agent_source = (ROOT / "codey" / "agent.py").read_text(encoding="utf-8")
        research_source = (ROOT / "codey" / "research" / "runner.py").read_text(encoding="utf-8")
        task_runner_source = (ROOT / "codey" / "task_runner.py").read_text(encoding="utf-8")
        tool_source = (ROOT / "codey" / "tool_runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("class StepResult", agent_source)
        self.assertNotIn("def trace_call", agent_source)
        self.assertNotIn("def _trace(", research_source)
        self.assertNotIn("def _trace_call", task_runner_source)
        self.assertNotIn("compatibility ``tool_*``", tool_source)


if __name__ == "__main__":
    unittest.main()
