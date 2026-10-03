from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from codey.app.context import AppContext
from codey.operations.task_run import TaskRunDeps
from codey.operations.task_state import TaskSubmissionStores

ROOT = Path(__file__).resolve().parents[1]
_STORE_FIELDS = (
    "project_facts",
    "work_checkpoints",
    "workspace_revisions",
    "run_ledgers",
    "run_traces",
    "evidence_ledgers",
    "managed_outputs",
    "knowledge_store",
    "runtime_mutations",
    "runtime_effects",
)


def _stores() -> TaskSubmissionStores:
    return TaskSubmissionStores(
        project_facts=object(),
        work_checkpoints=object(),
        workspace_revisions=object(),
        run_ledgers=object(),
        run_traces=object(),
        evidence_ledgers=object(),
        managed_outputs=object(),
        knowledge_store=object(),
        runtime_mutations=object(),
        runtime_effects=object(),
    )


def test_submission_state_exposes_one_typed_resource_bundle() -> None:
    source = (ROOT / "codey" / "operations" / "task_state.py").read_text(encoding="utf-8")
    assert "def task_submission_stores" in source
    for field in _STORE_FIELDS:
        assert f"def {field}" not in source


def test_app_context_builds_a_fresh_bundle_from_current_store_handles(tmp_path: Path) -> None:
    context = AppContext(tmp_path)
    try:
        first = context.build_task_submission_stores()
        assert first.project_facts is context.project_facts
        assert first.work_checkpoints is context.work_checkpoints
        assert first.runtime_mutations is context.runtime_mutations

        replacement = object()
        context.knowledge_store = replacement
        second = context.build_task_submission_stores()
        assert second.knowledge_store is replacement
    finally:
        context.close()


def test_task_run_deps_factory_expands_the_bundle_once() -> None:
    state = SimpleNamespace()
    stores = _stores()
    deps = TaskRunDeps.from_submission_stores(
        state=state,
        stores=stores,
        agent_run=lambda: None,
        collect_changes=lambda: None,
        run_review=lambda: None,
        capture_provider_failure=lambda: None,
    )
    for field in _STORE_FIELDS:
        assert getattr(deps, field) is getattr(stores, field)


def test_submission_entrypoints_use_the_bundle_factory() -> None:
    for relative in ("codey/app/task_submit.py", "codey/app/headless_runner.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "from_submission_stores" in source
        for field in _STORE_FIELDS:
            assert f"{field}=state.{field}" not in source


def test_factory_is_a_classmethod_with_explicit_store_input() -> None:
    signature = inspect.signature(TaskRunDeps.from_submission_stores)
    assert "stores" in signature.parameters
    assert signature.parameters["stores"].kind is inspect.Parameter.KEYWORD_ONLY
