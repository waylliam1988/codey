from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from codey.knowledge.store import KnowledgeStore
from codey.operations.task_state import TaskState
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore


@dataclass(frozen=True)
class ResearchFlowDeps:
    state: TaskState
    knowledge_store: KnowledgeStore | None
    search_factory: Callable[[], object]
    managed_outputs: ManagedOutputStore | None = None
    runtime_mutations: RuntimeMutationLine | None = None
