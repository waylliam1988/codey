"""Worker progress is an artifact on the existing effect, never a settlement."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from codey.automation.browser_binding import BrowserBinding
from codey.automation.browser_invocation import BrowserInvocation
from codey.automation.browser_session import BrowserSession
from codey.durable.session import DurableSession
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer


def browser_invocation(effect_ref: str, *, durable: DurableSession | None, store: ManagedOutputStore | None,
                       session_id: str, run_id: str) -> BrowserInvocation:
    progress: Callable[[JsonObject], None] | None = None
    if durable is not None and store is not None:
        def record(value: JsonObject) -> None:
            if value.get("effect_ref") != effect_ref:
                raise ValueError("browser progress belongs to another effect")
            reference = store.write_json_artifact(session_id=session_id, run_id=run_id, payload=value)
            artifact = store.json_artifact_record(reference, owner_task_id=run_id, owner_effect_id=effect_ref,
                                                  metadata={"kind": "browser_progress", "observed_at_ns": time.time_ns()})
            durable.register_artifacts(run_id, (artifact,))
        progress = record
    return BrowserInvocation(effect_ref, progress)


def restore_browser_progress(browser: BrowserSession, *, durable: DurableSession, store: ManagedOutputStore, run_id: str) -> None:
    if browser.resource_binding() is not None:
        return
    artifacts = sorted(durable.artifacts_for_task(run_id), key=lambda row: (
        require_json_integer(row.metadata.get("observed_at_ns", 0), "observed_at_ns")), reverse=True)
    restored_targets: set[str] = set()
    restored_binding = False
    for artifact in artifacts:
        if artifact.metadata.get("kind") != "browser_progress":
            continue
        reference = artifact.metadata.get("ref")
        if not isinstance(reference, Mapping):
            raise ValueError("browser progress lacks its original artifact reference")
        value = decode_json_object(store.read_json_artifact(dict[str, object](decode_json_object(reference))))
        document = value.get("document")
        if (isinstance(document, dict) and isinstance(target := document.get("target_id"), str)
                and target not in restored_targets and value.get("input_state") is not None):
            browser.restore_input_state(value)
            restored_targets.add(target)
        if not restored_binding and value.get("binding") is not None:
            browser.restore_binding(BrowserBinding.read(value["binding"]))
            restored_binding = True
