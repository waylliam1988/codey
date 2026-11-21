"""Synchronous task-browser facade; Playwright objects live only in the child."""
from __future__ import annotations

import time
from collections.abc import Iterable
from uuid import uuid4

from playwright.async_api import FilePayload

from codey.automation.browser_binding import BrowserBinding
from codey.automation.browser_contracts import BrowserAction, BrowserCommand, BrowserCompletion, BrowserConfig
from codey.automation.browser_contracts import BrowserToolOutput as BrowserToolOutput
from codey.automation.browser_document import normalize_element_ref
from codey.automation.browser_invocation import current_invocation
from codey.automation.browser_ipc import BrowserPacket
from codey.automation.browser_process import BrowserProcess, NetworkPolicy, Progress
from codey.automation.browser_query import QueryObservationError
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object


class BrowserSession:
    def __init__(self, config: BrowserConfig | None = None, *, owner_task_id: str = "",
                 network_policy: NetworkPolicy | None = None) -> None:
        self.owner_task_id = owner_task_id or uuid4().hex
        self.resource_id = uuid4().hex
        self._process = BrowserProcess(config or BrowserConfig(headless=False),
            owner_task_id=self.owner_task_id, resource_id=self.resource_id, network_policy=network_policy)
        self._recovery_checked = False
        self._leases = 0

    def retain(self) -> None:
        self._leases += 1

    def attach_owner(self, owner_task_id: str) -> None:
        self._process.attach_owner(owner_task_id)
        self.owner_task_id = owner_task_id

    def release(self) -> None:
        self._leases = max(0, self._leases - 1)
        if not self._leases:
            self.close()

    def close(self) -> None:
        if not self._leases:
            self._process.close()

    def prepare_from_receipts(self, results: Iterable[ToolResult]) -> None:
        if self._recovery_checked:
            return
        latest: BrowserBinding | None = None
        for result in results:
            if (result.call.name.startswith("browser_") or result.call.name == "open_url") and "browser_resource" in result.audit:
                latest = BrowserBinding.read(result.audit["browser_resource"])
        if latest is not None and self._process.binding is None:
            self._process.restore(latest)
        self._recovery_checked = True

    def restore_binding(self, binding: BrowserBinding) -> None:
        self._process.restore(binding)

    def restore_input_state(self, progress: JsonObject) -> None:
        self._process.observe_input_state(progress)

    @property
    def generation(self) -> int:
        return self._process.generation

    def resource_binding(self) -> JsonObject | None:
        binding = self._process.binding
        return binding.payload() if binding is not None else None

    def invoke(self, action: BrowserAction, arguments: JsonObject | None = None, *,
               binary: bytes = b"", effect_ref: str = "", progress: Progress | None = None) -> BrowserPacket:
        cancellation.check()
        deadline = min(time.monotonic() + 45, cancellation.current_deadline() or float("inf"))
        admitted = current_invocation()
        if admitted is not None:
            effect_ref, progress = admitted.effect_ref, admitted.progress
        command = BrowserCommand(uuid4().hex, action, arguments or {}, self.owner_task_id, self.resource_id,
                                 effect_ref, deadline,
                                 target_id=str(expected.get("target_id", "")) if isinstance(expected := (arguments or {}).get("expected_document"), dict) else "",
                                 frame_id=str(expected.get("frame_id", "")) if isinstance(expected, dict) else "")
        return self._process.call(command, binary=binary, progress=progress)

    def action(self, action: BrowserAction, arguments: JsonObject | None = None, *, binary: bytes = b"") -> BrowserToolOutput:
        packet = self.invoke(action, arguments, binary=binary)
        return BrowserToolOutput.read(packet.message["value"], binary=packet.blob)

    def _data(self, action: BrowserAction, arguments: JsonObject | None = None) -> JsonValue:
        packet = self.invoke(action, arguments)
        if BrowserCompletion(str(packet.message["completion"])) == BrowserCompletion.FAILED:
            output = BrowserToolOutput.read(packet.message["value"])
            if action == BrowserAction.QUERY_RESULT:
                raise QueryObservationError(str(output.observation["reason"]), output.model_text)
            raise RuntimeError(output.model_text)
        return packet.message["value"]

    def document_identity(self) -> JsonObject | None:
        if self._process.binding is None:
            return None
        value = self._data(BrowserAction.DOCUMENT)
        return decode_json_object(value) if value is not None else None

    def capture_wait_binding(self) -> JsonObject:
        document = self.document_identity()
        if document is None:
            raise ValueError("background browser wait requires an identified original page")
        return {"browser_resource": self.resource_binding(), "browser_document": document}

    def snapshot(self, *, max_elements: int = 80, max_text: int = 12000, file_inputs_only: bool = False,
                 scope_ref: str = "", selector: str = "", base_revision: int | None = None) -> BrowserToolOutput:
        args: JsonObject = {"max_elements": max_elements, "max_text": max_text,
            "file_inputs_only": file_inputs_only, "scope_ref": scope_ref, "selector": selector}
        if base_revision is not None:
            args["base_revision"] = base_revision
        return self.action(BrowserAction.SNAPSHOT, args)

    def click(self, ref: str, *, query: bool = False, result_ref: str = "") -> BrowserToolOutput:
        return self.action(BrowserAction.CLICK, {"ref": normalize_element_ref(ref), "query": query, "result_ref": result_ref})

    def fill(self, ref: str, value: str) -> BrowserToolOutput:
        return self.action(BrowserAction.FILL, {"ref": normalize_element_ref(ref), "value": value})

    def matches_query_control(self, ref: str, baseline: JsonObject) -> bool:
        value = self._data(BrowserAction.MATCH_QUERY, {"ref": normalize_element_ref(ref), "baseline": baseline})
        if type(value) is not bool or not isinstance(value, bool):
            raise ValueError("query target identity unavailable")
        return value

    def scroll(self, direction: str, amount: int) -> BrowserToolOutput:
        return self.action(BrowserAction.SCROLL, {"direction": direction, "amount": amount})

    def wait(self, condition: str, value: str, timeout_ms: int, *,
             expected_document: JsonObject | None = None) -> BrowserToolOutput:
        return self.action(BrowserAction.WAIT, {"condition": condition, "value": value, "timeout_ms": timeout_ms,
            "expected_document": (expected_document if expected_document is not None else
                None if condition in {"dialog", "popup", "download"} else self.document_identity())})

    def navigate(self, url: str) -> BrowserToolOutput:
        return self.action(BrowserAction.NAVIGATE, {"url": url})

    def evaluate(self, code: str) -> BrowserToolOutput:
        return self.action(BrowserAction.EVALUATE, {"code": code})

    def tab_list(self) -> BrowserToolOutput:
        return self.action(BrowserAction.TAB_LIST)

    def tab_new(self, url: str) -> BrowserToolOutput:
        return self.action(BrowserAction.TAB_NEW, {"url": url})

    def tab_switch(self, index: int) -> BrowserToolOutput:
        return self.action(BrowserAction.TAB_SWITCH, {"index": index})

    def upload(self, ref: str, file: FilePayload, *, expected_document: JsonObject | None = None,
               target: JsonObject | None = None) -> BrowserToolOutput:
        args: JsonObject = {"ref": normalize_element_ref(ref), "name": file["name"], "mime_type": file["mimeType"],
                            "expected_document": expected_document}
        if target is not None:
            args["target"] = target
        return self.action(BrowserAction.UPLOAD, args, binary=file["buffer"])

    def download(self, ref: str) -> BrowserToolOutput:
        return self.action(BrowserAction.DOWNLOAD, {"ref": normalize_element_ref(ref)})

    def capture(self) -> dict[str, object]:
        value = self._data(BrowserAction.CAPTURE)
        return dict(decode_json_object(value))

    def inspect_state(self, expression: str) -> JsonValue:
        """Passive controller observation: Chromium rejects expressions with side effects."""
        return self._data(BrowserAction.PEEK, {"expression": expression})

    def discard_references(self) -> None:
        """A user assistance boundary invalidates observations, never the page."""
        self._data(BrowserAction.RESET_REFS)

    def record_regions(self, labels: tuple[str, ...]) -> list[JsonObject]:
        value = self._data(BrowserAction.RECORDS, {"labels": list(labels)})
        if not isinstance(value, list):
            raise ValueError("invalid observed record regions")
        return [decode_json_object(row) for row in value]

    def capture_query_result(self, baseline: JsonObject, *, pending: str) -> dict[str, object]:
        value = self._data(BrowserAction.QUERY_RESULT, {"baseline": baseline})
        return dict(decode_json_object(value))

    def inspect_details(self, document: JsonObject, task: str, selected: JsonObject | None = None) -> JsonObject | None:
        value = self._data(BrowserAction.DISCLOSURE, {"document": document, "task": task, "selected": selected})
        return decode_json_object(value) if value is not None else None
