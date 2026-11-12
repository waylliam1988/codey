"""Page prerequisites and exact receipt delivery for single-page uploads."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.automation.browser_session import BrowserToolOutput
from codey.operations.browser_completion import committed_results
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import _browser_body, require_tool_success
from codey.runtime.core.models import ToolResult
from codey.storage.input_attachments import InputAttachmentStore
from codey.task.web_intent import source_url_key
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, decode_json_object

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.policies.task_policy import TaskPolicy
    from codey.storage.managed_outputs import ManagedOutputStore

_UPLOAD = re.compile(r"上传|\b(?:upload|browser_upload)\b", re.I)
_NEGATED = re.compile(r"(?:不要|禁止|无需|不用|不必|勿)\s*(?:再\s*)?$|\b(?:do not|don't|never|without)\s*$", re.I)
_REPORT_HASH = re.compile(
    r"(?:报告|汇报|告诉我|返回|给出|告知|列出|提供|输出|\b(?:report|return|provide|give|include|show|list)\b)"
    r"[^。！？!?;；\n]{0,240}\bsha[ -]?256\b", re.I | re.ASCII)
_PAGE_TEXT = re.compile(r"内容标记|页面(?:正文|文本|显示)|\b(?:content marker|page text)\b", re.I)
_ATTACHMENT = re.compile(r"\batt_[0-9a-f]{64}\b")
_NAVIGATE = require_tool_success("browser_navigate")
_READ = require_tool_success("browser_read")
_PAGE_RECEIPTS = frozenset({"open_url", "browser_navigate"})
_SELECTION_RECEIPTS = frozenset({"browser_upload", "browser_read"})
UPLOAD_RECEIPTS = _SELECTION_RECEIPTS | {"browser_navigate"}
UPLOAD_ACTION = "upload"
STRUCTURED_UPLOAD_TOOLS = frozenset({"browser_navigate", "browser_snapshot", "browser_upload", "browser_wait", "browser_read"})


class UploadPageUnavailable(ValueError):
    """The current document has no eligible opening receipt."""


@dataclass(frozen=True)
class UploadScope:
    url: str
    attachment: str
    navigate: bool
    report_hash: bool
    read_page: bool
    chinese: bool


def upload_scope(session: TaskSession) -> UploadScope | None:
    return task_upload_scope(session.task_kind, session.task_text, session.required_source_urls, session.policy)


def task_upload_scope(task_kind: str, task: str, urls: frozenset[str], policy: TaskPolicy) -> UploadScope | None:
    if task_kind != "chat" or policy.strict_research or len(urls) != 1 or not policy.allows("browser.effect"):
        return None
    task = re.sub(r"```[\s\S]*?```", "", task)
    actions = list(_UPLOAD.finditer(task))
    attachments = set(_ATTACHMENT.findall(task))
    if not actions or any(_NEGATED.search(task[:match.start()]) for match in actions) or len(attachments) > 1:
        return None
    checks = policy.required_checks
    return UploadScope(next(iter(urls)), next(iter(attachments), ""),
        _NAVIGATE in checks or re.search(r"\bbrowser_navigate\b", task) is not None,
        _REPORT_HASH.search(task) is not None, _READ in checks or _PAGE_TEXT.search(task) is not None,
        bool(re.search(r"[\u3400-\u9fff]", task)))


def _object(properties: dict[str, object], *required: str) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_LABEL = {"type": "string", "minLength": 1, "maxLength": 240}
UPLOAD_TARGET = {"anyOf": [_object({key: _LABEL}, key) for key in ("id", "name")]}


def upload_phase(results: Mapping[str, ToolResult]) -> str:
    phase = "observe"
    for result in results.values():
        name = result.call.name
        if name == "browser_upload":
            if result.ok:
                phase = "read"
            elif isinstance(value := result.canonical.get("value"), dict) and value.get("reason") == "file_observation_expired":
                phase = "observe"
        elif result.ok and name == "browser_snapshot" and phase != "read":
            value = result.canonical.get("value")
            controls = value.get("file_controls") if isinstance(value, dict) else None
            phase = "select" if isinstance(controls, list) and any(
                isinstance(row, dict) and row.get("disabled") is False for row in controls) else "observe"
        elif result.ok and name in {"browser_navigate", "browser_tab_new", "browser_tab_switch"}:
            phase = "observe"
    return phase


def structured_upload_spec(spec: ToolSpec, scope: UploadScope, phase: str) -> ToolSpec:
    if spec.name == "browser_snapshot":
        return freeze_spec_parameters(replace(spec, parameters=(*spec.parameters, ("file_inputs_only", {"type": "boolean"}))))
    if spec.name == "browser_upload":
        return freeze_spec_parameters(replace(spec, parameters=(*spec.parameters, ("target", UPLOAD_TARGET)),
                                              required=("attachment_id",)))
    if spec.name != "codemode":
        return spec
    attachment: dict[str, object] = {"type": "string", "pattern": "^att_[a-f0-9]{64}$"}
    if scope.attachment:
        attachment["enum"] = [scope.attachment]
    observe = _object({"action": {"type": "string", "enum": ["observe"]},
                       "url": {"type": "string", "enum": [scope.url]}}, "action")
    select = _object({"action": {"type": "string", "enum": ["select"]}, "target": UPLOAD_TARGET,
                      "attachment_id": attachment}, "action", "target", "attachment_id")
    read = _object({"action": {"type": "string", "enum": ["read"]}, "wait_for_text": _LABEL}, "action")
    options = {"observe": observe, "select": select, "read": read}[phase]
    return freeze_spec_parameters(replace(spec, parameters=(*spec.parameters, (UPLOAD_ACTION, options)), required=()))


def upload_model_spec(spec: ToolSpec) -> ToolSpec:
    parameters = tuple(row for row in spec.parameters if row[0] == UPLOAD_ACTION)
    if spec.name != "codemode" or not parameters:
        return spec
    return replace(spec, parameters=parameters, required=(UPLOAD_ACTION,), declarations="", json_examples=(),
        description="Structured attachment selection on the authorized webpage. Use the action offered in this turn's "
        "schema. Observe lists file controls. Add the authorized URL only when the page needs opening. Select accepts "
        "a unique name or an observed ID. If names repeat, choose an observed ID. Read accepts wait_for_text when "
        "the task calls for a page marker.")


def lower_upload_action(args: JsonObject) -> JsonObject:
    if set(args) != {UPLOAD_ACTION}:
        raise ValueError("Use one structured upload action without JavaScript arguments.")
    action = decode_json_object(args[UPLOAD_ACTION])
    if action["action"] == "observe":
        code = ('if (inputs.url) unwrap(await tools.browser_navigate({url:inputs.url})); '
                'const s=unwrap(await tools.browser_snapshot({file_inputs_only:true})); '
                'return {controls:s.file_controls,complete:!s.elements_truncated};')
    elif action["action"] == "select":
        code = "return unwrap(await tools.browser_upload({target:inputs.target,attachment_id:inputs.attachment_id}));"
    else:
        code = ('if (inputs.wait_for_text) unwrap(await tools.browser_wait({condition:"text",'
                'value:inputs.wait_for_text,timeout_ms:5000})); return unwrap(await tools.browser_read({}));')
    return {"code": code, "inputs": action}


def matches_document(document: object, url: str) -> bool:
    return (isinstance(document, dict) and isinstance(loader := document.get("loader_id"), str) and bool(loader)
            and isinstance(observed := document.get("url"), str) and source_url_key(observed) == url)


def browser_opened(result: ToolResult, url: str) -> bool:
    acquisition = result.canonical.get("source_acquisition")
    attempts = acquisition.get("attempts") if isinstance(acquisition, dict) else None
    requested = result.call.args.get("url")
    if (not result.ok or not isinstance(requested, str) or source_url_key(requested) != url
            or not isinstance(attempts, list) or not attempts or not isinstance(last := attempts[-1], dict)):
        return False
    return (last.get("backend") == "browser" and last.get("status") == "ok" and last.get("observation") == "content"
            and isinstance(final := last.get("final_url"), str) and source_url_key(final) == url)


def upload_page_ready(scope: UploadScope, document: JsonObject | None, results: Mapping[str, ToolResult]) -> bool:
    if not matches_document(document, scope.url):
        return False
    for result in results.values():
        if not result.ok or result.call.name not in _PAGE_RECEIPTS or result.audit.get("browser_document") != document:
            continue
        if result.call.name == "open_url":
            if not scope.navigate and browser_opened(result, scope.url):
                return True
        else:
            url = result.call.args.get("url")
            value = result.canonical.get("value")
            if (isinstance(url, str) and source_url_key(url) == scope.url and isinstance(value, dict)
                    and type(status := value.get("http_status")) is int and 200 <= status < 400):
                return True
    return False


def verified_upload_document(session: TaskSession | None, browser: BrowserSession) -> JsonObject:
    if session is None or (scope := upload_scope(session)) is None:
        raise ValueError("A structured upload requires an authorized target page.")
    document = browser.document_identity()
    if document is not None:
        with session.projection_lock:
            if upload_page_ready(scope, document, session._memory_results):
                return document
    raise UploadPageUnavailable("Open the authorized upload page, then observe its file controls before selecting the attachment.")


def select_attachment(session: TaskSession | None, browser: BrowserSession, outputs: ManagedOutputStore | None,
                      session_id: str, args: JsonObject) -> BrowserToolOutput:
    if outputs is None or not session_id:
        raise ValueError("task-scoped attachment storage unavailable")
    identity = str(args.get("attachment_id") or "")
    ref = str(args.get("ref") or "")
    target = decode_json_object(args["target"]) if "target" in args else None
    if (not ref) == (target is None):
        raise ValueError("Supply either a snapshot ref or one observed file-control target.")
    scope = upload_scope(session) if session is not None else None
    document = None
    if scope is not None:
        if scope.attachment and scope.attachment != identity:
            raise ValueError("Select the attachment identified by the task.")
        try:
            document = verified_upload_document(session, browser)
        except UploadPageUnavailable as exc:
            if target is None:
                raise
            return BrowserToolOutput(f"ERROR: {exc}", False, {"action": "upload", "ref": "",
                "delivery": "not_delivered", "reason": "file_observation_expired"})
    elif target is not None:
        raise ValueError("A structured upload requires an authorized target page.")
    file = InputAttachmentStore(outputs.root.parent, session_id).read_file(identity)
    output = browser.upload(ref, {"name": file.name, "mimeType": file.media_type, "buffer": file.data},
                            expected_document=document, target=target)
    return replace(output, observation={**output.observation, "attachment_id": identity})


def audit_upload_open(result: ToolResult, session: TaskSession | None, browser: BrowserSession | None) -> ToolResult:
    scope = upload_scope(session) if session is not None else None
    if browser is None or scope is None or scope.navigate or not browser_opened(result, scope.url):
        return result
    document = browser.document_identity()
    if not matches_document(document, scope.url):
        return result
    audit = {**result.audit, "browser_document": document}
    binding = browser.resource_binding()
    if binding is not None:
        audit["browser_resource"] = binding
    return replace(result, audit=audit)


@dataclass(frozen=True)
class UploadEvidence:
    name: str
    sha256: str
    text: str

    def render(self, *, chinese: bool) -> str:
        lines = ([f"已选择附件：{self.name}", f"选择回执 SHA256：{self.sha256}"] if chinese else
                 [f"Attachment selected: {self.name}", f"Selection receipt SHA256: {self.sha256}"])
        if self.text:
            lines.extend(["", "选择后的页面内容：" if chinese else "Page content after selection:", self.text])
        return "\n".join(lines)


def upload_evidence(session: TaskSession, scope: UploadScope, document: JsonObject | None) -> UploadEvidence | str:
    if not matches_document(document, scope.url):
        return "Return to the target upload page and verify its state before completing."
    selection: tuple[str, str] | None = None
    body = ""
    for _, result in committed_results(session.executed, session._memory_results, _SELECTION_RECEIPTS):
        if result.audit.get("browser_document") != document:
            continue
        if result.call.name == "browser_upload":
            value = result.canonical.get("value")
            if (not result.truncated and isinstance(value, dict) and value.get("delivery") == "verified"
                    and (not scope.attachment or value.get("attachment_id") == scope.attachment)
                    and isinstance(name := value.get("name"), str) and name
                    and isinstance(digest := value.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", digest)):
                selection, body = (name, digest), ""
        elif result.call.name == "browser_read" and selection is not None:
            try:
                body = _browser_body(result) or ""
            except (KeyError, TypeError, ValueError):
                body = ""
    if selection is None:
        return "Select the requested attachment on the target page and obtain its successful upload receipt."
    if scope.read_page and not body:
        return "Read the complete target page after selecting the attachment. Reuse the existing upload receipt."
    return UploadEvidence(*selection, body if scope.read_page else "")
