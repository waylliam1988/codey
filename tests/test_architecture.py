from __future__ import annotations

import ast
import json
import subprocess
import sys
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
        imports = imported_modules(ROOT / "codey" / "agents" / "runner.py")

        self.assertNotIn("playwright.sync_api", imports)
        self.assertNotIn("codey.automation.browser", imports)
        self.assertNotIn("codey.providers.web_drivers.deepseek", imports)
        self.assertNotIn("codey.providers.web_drivers.qwen", imports)
        self.assertNotIn("codey.providers.web_drivers.stepfun", imports)
        self.assertNotIn("codey.providers.web_drivers.glm", imports)
        self.assertIn("codey.providers", imports)
        self.assertIn("codey.protocols", imports)

    def test_orchestrators_create_providers_instead_of_browser_sessions(self) -> None:
        paths = (
            ("cli.py", ROOT / "codey" / "app" / "cli.py"),
            ("server.py", ROOT / "codey" / "app" / "server.py"),
            ("task_runner.py", ROOT / "codey" / "app" / "task_runner.py"),
        )
        for name, path in paths:
            with self.subTest(name=name):
                imports = imported_modules(path)
                self.assertNotIn("codey.automation.browser", imports)
                self.assertNotIn("codey.providers.web_drivers.deepseek", imports)
                self.assertNotIn("codey.providers.web_drivers.qwen", imports)
                self.assertNotIn("codey.providers.web_drivers.stepfun", imports)
                self.assertNotIn("codey.providers.web_drivers.glm", imports)

    def test_http_server_delegates_task_orchestration(self) -> None:
        imports = imported_modules(ROOT / "codey" / "app" / "server.py")
        source = (ROOT / "codey" / "app" / "server.py").read_text(encoding="utf-8")

        self.assertIn("codey.app.task_runner", imports)
        self.assertNotIn("on_shell_request(cwd_rel", source)
        self.assertNotIn("conversation.prepare_model_handoff", source)

    def test_task_runner_has_no_http_dependency(self) -> None:
        imports = imported_modules(ROOT / "codey" / "app" / "task_runner.py")

        self.assertNotIn("http.server", imports)
        self.assertNotIn("codey.app.server", imports)

    def test_research_pipeline_owns_iteration_boundary_without_legacy_seams(self) -> None:
        pipeline = ROOT / "codey" / "research" / "pipeline.py"
        task_runner = ROOT / "codey" / "app" / "task_runner.py"
        research_runner = ROOT / "codey" / "research" / "runner.py"
        pipeline_source = pipeline.read_text(encoding="utf-8")
        task_runner_source = task_runner.read_text(encoding="utf-8")
        research_runner_source = research_runner.read_text(encoding="utf-8")

        self.assertIn("ResearchIterationRun", pipeline_source)
        self.assertIn("ResearchIterationRun", task_runner_source)
        self.assertNotIn("codey.app.task_runner", imported_modules(pipeline))
        self.assertNotIn("codey.app.server", imported_modules(pipeline))
        self.assertNotIn("_run_research_task", task_runner_source)
        self.assertNotIn("close_search", pipeline_source)
        self.assertNotIn("runtime_tools", research_runner_source)

    def test_ghost_runtime_has_no_provider_browser_tool_or_research_dependency(self) -> None:
        forbidden = {
            "torch",
            "transformers",
            "codey.automation.browser",
            "codey.providers",
            "codey.toolchain.runtime",
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
            "codey.automation.browser",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.research.runner",
            "codey.research.tools",
        }
        self.assertTrue(
            forbidden_affinity.isdisjoint(affinity_imports),
            sorted(forbidden_affinity & affinity_imports),
        )

        research_imports = imported_modules(ROOT / "codey" / "research" / "runner.py")
        permission_imports = imported_modules(ROOT / "codey" / "policies" / "permissions.py")
        repair_source = (ROOT / "codey" / "repairs" / "adapter_repair.py").read_text(encoding="utf-8")
        tool_runtime_imports = imported_modules(ROOT / "codey" / "toolchain" / "runtime.py")

        self.assertNotIn("codey.ghost.affinity", research_imports)
        self.assertNotIn("codey.ghost.affinity", permission_imports)
        self.assertNotIn("affinity", repair_source.casefold())
        self.assertNotIn("codey.ghost", tool_runtime_imports)

    def test_context_epoch_is_projection_only_leaf(self) -> None:
        # Context Epoch projects admission metadata over already-rendered
        # sources; it must stay a stdlib-only leaf with no runtime imports
        # and no I/O of its own.
        path = ROOT / "codey" / "workspace" / "context_epoch.py"
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
        imports = imported_modules(ROOT / "codey" / "runtime" / "prompt_envelope.py")
        forbidden = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.research.runner",
            "codey.ghost",
        }

        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_capability_registry_is_metadata_only(self) -> None:
        path = ROOT / "codey" / "policies" / "capability_registry.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.research.runner",
            "codey.app.server",
            "codey.app.task_runner",
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
        source = (ROOT / "codey" / "app" / "task_runner.py").read_text(encoding="utf-8")

        self.assertIn("self.capabilities = capabilities", source)
        self.assertEqual(source.count("self.capabilities"), 1)
        self.assertNotIn("if capabilities", source)
        self.assertNotIn("if self.capabilities", source)

    def test_builtin_profiles_module_is_gone(self) -> None:
        # The metadata-only catalog never influenced any decision and was
        # removed ahead of 0.4.x instead of shipping dead surface.
        self.assertFalse((ROOT / "codey" / "builtin_profiles.py").exists())
        source = (ROOT / "codey" / "app" / "task_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("builtin_profiles", source)
        server_source = (ROOT / "codey" / "app" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("builtin_profiles", server_source)

    def test_action_policy_is_not_runtime_or_plugin_host(self) -> None:
        path = ROOT / "codey" / "policies" / "action.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.app.task_runner",
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
        path = ROOT / "codey" / "runs" / "details.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.app.task_runner",
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

    def test_edit_scope_is_stdlib_leaf(self) -> None:
        # The edit-scope vocabulary has no runtime dependencies at all:
        # every consumer (proof, monitor, harness) shares one path
        # classification that cannot drag execution into a projection.
        path = ROOT / "codey" / "completion" / "edit_scope.py"
        imports = imported_modules(path)
        internal = sorted(name for name in imports if name == "codey" or name.startswith("codey."))
        self.assertEqual(internal, [])

    def test_edit_integrity_is_projection_leaf(self) -> None:
        path = ROOT / "codey" / "completion" / "edit_integrity.py"
        imports = imported_modules(path)
        forbidden = {
            "codey.automation.browser",
            "codey.providers",
            "codey.toolchain",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.app.task_runner",
            "codey.ghost",
            "codey.research",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))
        allowed_internal = {"codey.completion.edit_scope", "codey.utils.refs"}
        internal = {
            name
            for name in imports
            if name == "codey" or name.startswith("codey.")
        }
        self.assertTrue(internal.issubset(allowed_internal), sorted(internal - allowed_internal))

    def test_completion_decision_is_pure_projection(self) -> None:
        path = ROOT / "codey" / "completion" / "decision.py"
        imports = imported_modules(path)
        forbidden = {
            "codey.automation.browser",
            "codey.providers",
            "codey.toolchain",
            "codey.app.server",
            "codey.app.task_runner",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_research_object_model_is_projection_not_runtime(self) -> None:
        path = ROOT / "codey" / "research" / "object_model.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.app.task_runner",
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
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.app.server",
            "codey.app.task_runner",
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

    def test_source_domains_is_a_stdlib_data_leaf(self) -> None:
        # The shared host-domain tables have exactly one owner and no
        # behavior: both the capture-time classifier and the trust
        # projection consume them, so they must stay import-cycle-free.
        # codey.utils.refs is the one allowed import (the shared stdlib hostname
        # shape predicate); no other codey module, no I/O.
        path = ROOT / "codey" / "research" / "source_domains.py"
        imports = imported_modules(path)

        self.assertEqual(
            sorted(name for name in imports if name == "codey" or name.startswith("codey.")),
            ["codey.utils.refs"],
        )
        source = path.read_text(encoding="utf-8")
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
            "urlparse",
        ):
            self.assertNotIn(token, source)

    def test_research_review_and_local_context_do_not_import_tool_runtime(self) -> None:
        paths = [
            *(ROOT / "codey" / "research").glob("*.py"),
            ROOT / "codey" / "reviews" / "core.py",
            ROOT / "codey" / "reviews" / "coordinator.py",
            *(ROOT / "codey" / "ghost").glob("*.py"),
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertNotIn("codey.toolchain.runtime", imports)
                # managed_outputs sits on the runtime side of the boundary
                # (it imports tool_runtime), so research/review/ghost modules
                # must consume normalized metadata dicts instead.
                self.assertNotIn("codey.storage.managed_outputs", imports)

    def test_analysis_run_projection_stays_pure(self) -> None:
        analysis_run_imports = imported_modules(ROOT / "codey" / "research" / "analysis_run.py")
        lineage_imports = imported_modules(ROOT / "codey" / "research" / "artifact_lineage.py")
        capsule_imports = imported_modules(ROOT / "codey" / "research" / "reproducibility.py")
        forbidden = {
            "codey.runtime.events",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.app.task_runner",
            "codey.app.server",
        }
        for name, imports in (
            ("analysis_run", analysis_run_imports),
            ("artifact_lineage", lineage_imports),
            ("reproducibility", capsule_imports),
        ):
            with self.subTest(module=name):
                self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_research_behavior_modules_do_not_read_trace_or_ui_projections(self) -> None:
        # Projection-of-projection is forbidden: behavior-side research
        # modules consume canonical facts, never the trace/UI read models.
        forbidden = {
            "codey.runs.trace",
            "codey.runs.details",
            "codey.runs.ledger_projection",
        }
        paths = [
            ROOT / "codey" / "research" / "query_planner.py",
            ROOT / "codey" / "research" / "proof_quality.py",
            ROOT / "codey" / "research" / "brief_projection.py",
            ROOT / "codey" / "research" / "source_trust.py",
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden.isdisjoint(imports),
                    sorted(forbidden & imports),
                )

    def test_profile_source_trust_combination_has_single_owner(self) -> None:
        # Composing evidence profiles with source trust must live in exactly
        # one place. Today nothing combines them; if a consumer ever needs
        # to, it must become a dedicated owner module -- not another import
        # site that quietly grows policy logic.
        offenders = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imports = imported_modules(path)
            if (
                "codey.research.domain_profiles" in imports
                and "codey.research.source_trust" in imports
            ):
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_refs_and_redaction_are_stdlib_leaves(self) -> None:
        # The bounded ref vocabulary and redaction predicates are the shared
        # dialect of every refs-only read model (coding, research, future
        # experiment domains). They stay domain-neutral stdlib leaves so no
        # projection has to reach into another domain's namespace to speak.
        paths = (
            ("refs.py", ROOT / "codey" / "utils" / "refs.py"),
            ("redaction.py", ROOT / "codey" / "policies" / "redaction.py"),
        )
        for name, path in paths:
            with self.subTest(module=name):
                imports = imported_modules(path)
                self.assertEqual(
                    [item for item in imports if item == "codey" or item.startswith("codey.")],
                    [],
                )
                source = path.read_text(encoding="utf-8")
                for token in (
                    "write_text(",
                    "write_json",
                    "open(",
                    "eval(",
                    "exec(",
                    "subprocess",
                    "urllib",
                    "pathlib",
                ):
                    self.assertNotIn(token, source)

    def test_digest_helpers_are_not_imported_under_neutral_aliases(self) -> None:
        # AST-based so whitespace/quoting can never smuggle an aliased
        # import past a string scan again.
        offenders: list[str] = []
        watched = {"content_digest", "valid_digest_ref"}
        for path in sorted((ROOT / "codey").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path.relative_to(ROOT).as_posix()}: import {alias.name}"
                        for alias in node.names
                        if alias.asname == "_digest_ref"
                    )
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[-1] != "refs":
                        continue
                    offenders.extend(
                        f"{path.relative_to(ROOT).as_posix()}: from {node.module}"
                        f" import {alias.name} as _digest_ref"
                        for alias in node.names
                        if alias.name in watched and alias.asname == "_digest_ref"
                    )

        self.assertEqual(offenders, [])

    def test_completion_contract_modules_are_projection_only(self) -> None:
        # The completion contract is the Verified Completion Gate's pure core:
        # it derives proofs from facts handed to it, and must never reach
        # execution layers, providers, I/O, or model-visible surfaces. The
        # queue gate itself stays under the existing research import-boundary
        # test above.
        paths = (
            ROOT / "codey" / "completion" / "contract.py",
            ROOT / "codey" / "research" / "contract.py",
        )
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.runtime.events",
            "codey.app.server",
            "codey.app.task_runner",
            "codey.ghost",
            "codey.reviews.core",
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
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.app.server",
            "codey.app.task_runner",
            "codey.ghost",
            "codey.reviews.core",
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

    def test_regression_gate_is_projection_only(self) -> None:
        # The 0.4.11 regression gate is the evaluation spine's pure core: it
        # consumes existing projection payloads and emits bounded metrics,
        # observables, and verdicts. It must never reach execution layers,
        # providers, the A/B journal, or perform any I/O of its own.
        path = ROOT / "codey" / "research" / "regression_gate.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.runtime.events",
            "codey.app.server",
            "codey.app.task_runner",
            "codey.ghost",
            "codey.reviews.core",
            "codey.knowledge",
            "codey.runs.trace",
            "codey.runs.details",
            "ab_journal",
            "tests.manual.ab_journal",
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
            "requests.",
        )

        self.assertTrue(
            forbidden_imports.isdisjoint(imports),
            sorted(forbidden_imports & imports),
        )
        for token in forbidden_source:
            self.assertNotIn(token, source)

    def test_regression_gate_is_not_exported_from_research_package_init(self) -> None:
        # The gate stays a lazy sub-module import so importing the research
        # package surface does not eagerly load the evaluation spine.
        source = (ROOT / "codey" / "research" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("regression_gate", source)

    def test_production_never_imports_the_manual_layer(self) -> None:
        # Manual harnesses, journals, and benchmark tooling are experiment-
        # layer only; no production module may import them in either direction.
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            imports = imported_modules(path)
            bad = [
                name
                for name in imports
                if name.startswith("tests.")
                or name == "ab_journal"
                or name == "ab_harness_common"
                or name.endswith(".ab_journal")
            ]
            if bad:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {sorted(bad)}")
        self.assertEqual(offenders, [])

    def test_domain_profiles_is_a_stdlib_data_leaf(self) -> None:
        # Profiles are data only: no planner, no I/O, no codey imports at all.
        path = ROOT / "codey" / "research" / "domain_profiles.py"
        imports = imported_modules(path)

        self.assertEqual(
            [name for name in imports if name == "codey" or name.startswith("codey.")],
            [],
        )
        source = path.read_text(encoding="utf-8")
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
            "importlib",
        ):
            self.assertNotIn(token, source)

    def test_source_trust_and_brief_projection_stay_projection_only(self) -> None:
        # Source trust classifies sources; brief projection structures handoff
        # refs. Neither may fetch, delete evidence, reach execution layers, or
        # import the knowledge store (which consumes their outputs instead).
        forbidden_imports = {
            "codey.automation.browser",
            "codey.providers.web_drivers.deepseek",
            "codey.providers.web_drivers.qwen",
            "codey.providers.web_drivers.stepfun",
            "codey.providers.web_drivers.glm",
            "codey.providers",
            "codey.providers.controls",
            "codey.toolchain.runtime",
            "codey.storage.managed_outputs",
            "codey.runtime.events",
            "codey.app.server",
            "codey.app.task_runner",
            "codey.ghost",
            "codey.reviews.core",
            "codey.knowledge",
            "codey.research.runner",
            "codey.research.tools",
            "codey.research.connector_search",
            "urllib",
            "subprocess",
            "importlib",
            "pkgutil",
        }
        forbidden_source = (
            "eval(",
            "exec(",
            "write_text(",
            "write_json",
            "requests.",
        )
        for name in ("source_trust.py", "brief_projection.py"):
            path = ROOT / "codey" / "research" / name
            with self.subTest(module=name):
                imports = imported_modules(path)
                self.assertTrue(
                    forbidden_imports.isdisjoint(imports),
                    sorted(forbidden_imports & imports),
                )
                source = path.read_text(encoding="utf-8")
                for token in forbidden_source:
                    self.assertNotIn(token, source)

    def test_knowledge_never_imports_the_research_package(self) -> None:
        # Knowledge is a lower layer than Research: its Writer handoff consumes
        # the neutral section parser (codey.reviews.report_sections) instead of
        # reaching upward into codey.research, whose package __init__ eagerly
        # loads the runner/browser/pipeline stack.
        for path in sorted((ROOT / "codey" / "knowledge").glob("*.py")):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                imports = imported_modules(path)
                research_imports = {
                    name for name in imports if name.startswith("codey.research")
                }
                self.assertEqual(sorted(research_imports), [])

    def test_knowledge_brief_import_does_not_load_research_runtime(self) -> None:
        # Import-level isolation, checked in a clean interpreter: importing
        # the brief must not transitively load any research runtime module.
        script = (
            "import sys\n"
            "import codey.knowledge.brief\n"
            "loaded = [name for name in sys.modules if name.startswith('codey.research')]\n"
            "assert loaded == [], loaded\n"
            "print('isolated')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout={completed.stdout} stderr={completed.stderr}",
        )
        self.assertIn("isolated", completed.stdout)

    def test_report_sections_is_a_stdlib_leaf(self) -> None:
        # The shared section parser is domain-neutral: no codey imports and
        # no I/O, so both research and knowledge layers can own one parser.
        self._assert_stdlib_leaf(ROOT / "codey" / "reviews" / "report_sections.py")

    def test_citation_scanner_is_a_stdlib_leaf(self) -> None:
        # Citation scanning is shared by Research's done gate and Knowledge's
        # Writer handoff, so the scanner lives below both domains.
        self._assert_stdlib_leaf(ROOT / "codey" / "utils" / "citation_scanner.py")

    def _assert_stdlib_leaf(self, path: Path) -> None:
        imports = imported_modules(path)

        self.assertEqual(
            [name for name in imports if name == "codey" or name.startswith("codey.")],
            [],
        )
        source = path.read_text(encoding="utf-8")
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "subprocess",
            "urllib",
        ):
            self.assertNotIn(token, source)

    def test_ab_journal_is_manual_layer_only(self) -> None:
        # The A/B journal is manual-experiment tooling: production layers must
        # not consume it, and it must not depend on production orchestration.
        journal_path = ROOT / "tests" / "manual" / "ab_journal.py"
        journal_imports = imported_modules(journal_path)
        self.assertTrue(
            {"codey.runs.trace", "codey.research.evidence_ledger", "codey.app.task_runner", "codey.app.server"}.isdisjoint(
                journal_imports
            ),
            sorted(journal_imports),
        )

        consumers = [
            ROOT / "codey" / "runs" / "trace.py",
            *(ROOT / "codey" / "research").glob("*.py"),
            ROOT / "codey" / "app" / "task_runner.py",
            ROOT / "codey" / "app" / "server.py",
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

    def test_research_topic_continuity_is_pure_projection_leaf(self) -> None:
        # Topic continuity (0.4.12) is a read model over bounded local facts.
        # It must stay a stdlib-only leaf: no Ghost runtime import, no
        # providers, no I/O, no networking, and no evidence vocabulary of
        # its own (GhostHint != Evidence).
        path = ROOT / "codey" / "research" / "topic_continuity.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        self.assertEqual(
            sorted(name for name in imports if name == "codey" or name.startswith("codey.")),
            [],
        )
        forbidden_imports = {
            "subprocess",
            "urllib",
            "requests",
            "socket",
            "importlib",
            "pkgutil",
        }
        self.assertTrue(forbidden_imports.isdisjoint(imports), sorted(imports))
        for token in (
            "write_text(",
            "write_json",
            "open(",
            "eval(",
            "exec(",
            "evidence_refs",
            "web_search",
            "provider.send",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_research_stack_never_imports_ghost_runtime(self) -> None:
        # The Research pipeline consumes only the bounded continuity
        # projection handed to it; it must never reach into Ghost stores.
        for name in ("context.py", "pipeline.py", "runner.py", "topic_continuity.py"):
            imports = imported_modules(ROOT / "codey" / "research" / name)
            with self.subTest(module=name):
                ghost_imports = [
                    item for item in imports
                    if item == "codey.ghost" or item.startswith("codey.ghost.")
                ]
                self.assertEqual(sorted(ghost_imports), [])

    def test_stamped_capability_ids_are_registered_boundaries(self) -> None:
        # Every capability_id literal stamped onto a prompt section or context
        # source in production code must name a registered capability.
        from codey.policies.capability_registry import builtin_capability_registry

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
        agent_source = (ROOT / "codey" / "agents" / "runner.py").read_text(encoding="utf-8")
        research_source = (ROOT / "codey" / "research" / "runner.py").read_text(encoding="utf-8")
        task_runner_source = (ROOT / "codey" / "app" / "task_runner.py").read_text(encoding="utf-8")
        tool_source = (ROOT / "codey" / "toolchain" / "runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("class StepResult", agent_source)
        self.assertNotIn("def trace_call", agent_source)
        self.assertNotIn("def _trace(", research_source)
        self.assertNotIn("def _trace_call", task_runner_source)
        self.assertNotIn("compatibility ``tool_*``", tool_source)

    def test_run_operation_is_a_storage_leaf(self) -> None:
        # The durable run-operation counter (0.5.1) is persistence only:
        # stdlib plus the storage primitives, never agents, providers,
        # tools, server, ghost, or completion semantics.
        path = ROOT / "codey" / "run_operation.py"
        imports = imported_modules(path)

        internal = sorted(
            name for name in imports if name == "codey" or name.startswith("codey.")
        )
        self.assertEqual(
            internal,
            ["codey.storage.file_lock", "codey.storage.local_store"],
        )
        forbidden = {
            "codey.agents",
            "codey.app",
            "codey.completion",
            "codey.ghost",
            "codey.policies",
            "codey.providers",
            "codey.research",
            "codey.runs",
            "codey.toolchain",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

    def test_completion_repair_context_is_pure_projection_leaf(self) -> None:
        # The repair context (0.4.13) consumes an already-evaluated proof
        # payload; it must never import the completion contract (one
        # completion semantic owner), the verification module, or any
        # runtime layer. It is a stdlib + redaction leaf, nothing else.
        path = ROOT / "codey" / "completion" / "repair_context.py"
        imports = imported_modules(path)
        source = path.read_text(encoding="utf-8")

        allowed_internal = {"codey.policies.redaction"}
        internal = sorted(
            name for name in imports if name == "codey" or name.startswith("codey.")
        )
        self.assertTrue(set(internal) <= allowed_internal, sorted(internal))
        forbidden_imports = {
            "codey.completion.contract",
            "codey.completion.verification",
            "codey.app.task_runner",
            "codey.agents.runner",
            "codey.providers",
            "codey.toolchain.runtime",
            "codey.ghost",
            "subprocess",
            "urllib",
            "socket",
            "importlib",
        }
        self.assertTrue(forbidden_imports.isdisjoint(imports), sorted(imports & forbidden_imports))
        for token in (
            "write_text(",
            "open(",
            "eval(",
            "exec(",
            "build_completion_contract",
            "project_completion_proof",
            "provider.send",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_completion_repair_payload_vocabulary_is_closed(self) -> None:
        # The trace payload of a repair projection carries counts, classes,
        # reason codes and a digest only: there is no field that could hold
        # raw failure output, prompt text, or source bodies.
        from codey.completion.repair_context import project_repair_context

        projection = project_repair_context(
            proof={
                "status": "failed",
                "proof_id": "completion_proof:" + "a" * 16,
                "contract_id": "completion_contract:" + "b" * 16,
                "reason_codes": ["relevant_verification_failed"],
                "checks": [{
                    "check_id": "relevant_verification",
                    "status": "fail",
                    "reason_code": "relevant_verification_failed",
                }],
                "raw_stdout": "SHOULD_NEVER_APPEAR",
            },
            failure_class="product_failure",
            decisive_checks=[{
                "command": "pytest -q",
                "cwd": ".",
                "exit_code": 1,
                "result_summary": "1 failed",
            }],
        )
        payload = projection.to_payload()
        allowed_keys = {
            "schema_version",
            "kind",
            "context_source",
            "admitted",
            "failure_class",
            "detail",
            "check_count",
            "changed_file_count",
            "analysis_run_ref_count",
            "finding_ref_count",
            "summary_chars",
            "truncated",
            "reason_codes",
            "warnings",
            "digest",
            "proof_id",
            "contract_id",
            "refused_reason",
        }
        self.assertTrue(set(payload) <= allowed_keys, sorted(set(payload) - allowed_keys))
        self.assertNotIn("SHOULD_NEVER_APPEAR", json.dumps(payload))
        self.assertTrue(str(payload["digest"]).startswith("sha256:"))

    def test_no_repair_or_completion_managers_exist(self) -> None:
        # 0.4.13 closes the loop with pure projections and thin wiring; a
        # manager/planner/scheduler layer would reintroduce exactly the
        # runtime-that-thinks-for-the-model boundary Codey rejects.
        forbidden_names = (
            "RepairManager",
            "CompletionManager",
            "RepairRuntime",
            "RepairPlanner",
            "RepairCoordinator",
            "RepairPolicyEngine",
            "RepairScheduler",
            "MetaPlanner",
        )
        offenders: list[str] = []
        for path in sorted((ROOT / "codey").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            found = [name for name in forbidden_names if name in source]
            if found:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {found}")
        self.assertEqual(offenders, [])

    def test_completion_enforcement_has_explicit_stop_conditions(self) -> None:
        # The repair loop must be bounded by named stop conditions, never a
        # bare `while not complete`. Locks the v1 shape: one round max.
        source = (ROOT / "codey" / "app" / "task_runner.py").read_text(encoding="utf-8")
        self.assertIn("MAX_COMPLETION_REPAIR_ROUNDS = 1", source)
        self.assertIn("_COMPLETION_BLOCKED_NOTE", source)
        for reason in (
            "unobserved",
            "max_repair_rounds",
            "turn_budget_exhausted",
            "environment_failure",
            "provider_failure",
            "repair_context_unavailable",
            "repair_not_admitted",
        ):
            with self.subTest(reason=reason):
                self.assertIn(f'"{reason}"', source)

    def test_completion_and_repair_surfaces_never_become_model_tools(self) -> None:
        # 0.4.13 boundary lock: proofs, evidence, and the repair context are
        # runtime-owned projections. Neither protocol contract may grow a
        # tool for them, and neither surface may rename tools across domains:
        # coding keeps its read/write vocabulary and research keeps its own.
        from codey.research.tool_contract import TOOL_CONTRACTS as RESEARCH_CONTRACTS
        from codey.toolchain.definition import TOOL_DEFINITIONS, render_tool_contract

        self.assertEqual(
            {spec.name for spec in TOOL_DEFINITIONS},
            {
                "list_dir",
                "read_file",
                "read_files",
                "grep",
                "find_references",
                "parallel",
                "edit",
                "run",
                "shell",
                "done",
            },
        )
        self.assertEqual(
            set(RESEARCH_CONTRACTS),
            {
                "web_search",
                "open_url",
                "source_search",
                "knowledge_search",
                "knowledge_read",
                "knowledge_write",
                "knowledge_link",
                "done",
            },
        )
        rendered = render_tool_contract()
        forbidden_tokens = (
            "completion_proof",
            "completion_contract",
            "completion_repair_context",
            "repair_context",
            "evidence_ledger",
            "web_search",
            "open_url",
            "knowledge_write",
            "source_search",
        )
        for token in forbidden_tokens:
            with self.subTest(token=token):
                self.assertNotIn(token, rendered)

    def test_repair_context_is_rejected_as_an_unknown_model_tool(self) -> None:
        # Behavioral side of the same boundary: calling the repair context
        # through either codec is a typed unknown-tool error, not a tool.
        from codey.protocols.json_codec import JsonToolCodec
        from codey.research.protocols import JsonToolCodec as ResearchCodec
        from codey.research.tool_contract import PROTOCOL_UNKNOWN_TOOL

        payload = json.dumps({
            "tool": "completion_repair_context",
            "args": {"failure": "product"},
        })
        coding_plan = JsonToolCodec().parse(payload)
        self.assertEqual(coding_plan.calls, [])
        self.assertIsNone(coding_plan.control)
        self.assertIn("unknown tool: completion_repair_context", coding_plan.protocol_error)

        research_plan = ResearchCodec().parse(payload)
        self.assertEqual(research_plan.calls, [])
        self.assertIsNone(research_plan.control)
        self.assertEqual(research_plan.protocol_error_kind, PROTOCOL_UNKNOWN_TOOL)


if __name__ == "__main__":
    unittest.main()
