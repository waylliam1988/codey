"""Page prerequisites and exact receipt delivery for single-page uploads."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.automation.browser_session import BrowserToolOutput
from codey.operations.browser_completion import committed_results
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import has_explanation
from codey.policies.tool_requirement import (
    _browser_body,
    argument_digest,
    parse_tool_success_check,
    require_tool_success,
)
from codey.runtime.core.models import ToolResult
from codey.storage.input_attachments import InputAttachmentStore
from codey.task.web_intent import source_url_key
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, spec_for_tool, validate_args_against_spec
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object

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
_QUOTED_STATUS = re.compile(r'```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|「[^」\n]*」'
                            r"|(?<!\w)'[^'\n]*'(?!\w)")
_STATUS_CLAUSES = re.compile(r"[。！？；，\n.!?;,]|\bbut\b|但是|但", re.I)
_UPLOAD_ASSERTION = re.compile(
    r"已(?:经)?(?:成功)?上传(?:完成|成功)?|成功上传|上传(?:已(?:经)?)?(?:成功|完成)|"
    r"(?:服务器|服务端)(?:已(?:经)?(?:确认)?|确认)(?:成功)?(?:收到|接收|接受)|"
    r"\b(?:successfully\s+uploaded|uploaded\s+successfully|(?:was|has been|have been)\s+uploaded|"
    r"upload\s+(?:(?:is|was)\s+)?(?:complete(?:d)?|successful)|"
    r"(?:the\s+)?server\s+(?:(?:has|had|already|successfully)\s+)*(?:received|accepted|confirmed\s+receipt))\b", re.I)
_NEGATED_ASSERTION = re.compile(
    r"(?:没有|尚未|并未|未能|未|是否|不能确认|无法确认|不能证明|无法证明|不代表|不意味着)(?:文件|附件)?\s*$|"
    r"\b(?:not(?:\s+yet)?(?:\s+been)?|never|(?:cannot|can't)\s+(?:confirm|verify|prove)(?:\s+that)?|"
    r"no\s+evidence\s+that)\s*$", re.I)
_UNVERIFIED_ASSERTION = re.compile(r"^\s*(?:这一点|的状态)?(?:尚未|未经|未)(?:核验|验证|确认)")
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
    wait_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class UploadWaitPlan:
    waits: tuple[JsonObject, ...]
    missing: tuple[JsonObject, ...]

    @property
    def needs_arguments(self) -> bool:
        """A single text marker cannot cover incompatible or non-text predicates."""
        values = {argument_digest(args["value"]) for args in self.missing if "value" in args}
        return len(values) > 1 or any(
            any(key not in {"condition", "value", "timeout_ms"} for key in args)
            or args.get("condition", "text") != "text" or args.get("timeout_ms", 5000) != 5000
            for args in self.missing)


def upload_wait_plan(checks: tuple[str, ...], supplied: tuple[JsonObject, ...] = ()) -> UploadWaitPlan:
    """Plan executable waits and check every task argument predicate against the plan."""
    required: list[JsonObject] = []
    waits: list[JsonObject] = []
    for check in checks:
        rule = parse_tool_success_check(check)
        if rule is None or rule.tool != "browser_wait":
            continue
        expected = parse_json_object(rule.arguments_json)
        required.append(expected)
        args = dict(expected)
        args.setdefault("timeout_ms", 5000)
        if (isinstance(args.get("condition"), str) and isinstance(args.get("value"), str)
                and args["value"] and not validate_args_against_spec("browser_wait", args) and args not in waits):
            waits.append(args)
    for value in supplied:
        args = dict(value)
        args.setdefault("timeout_ms", 5000)
        if error := validate_args_against_spec("browser_wait", args):
            raise ValueError(error)
        if args not in waits:
            waits.append(args)
    missing = tuple(expected for expected in required if not any(all(
        key in wait and argument_digest(wait[key]) == argument_digest(value) for key, value in expected.items())
        for wait in waits))
    return UploadWaitPlan(tuple(waits), missing)


def upload_scope(session: TaskSession) -> UploadScope | None:
    return task_upload_scope(session.task_kind, session.task_text, session.required_source_urls, session.policy)


def bound_upload_explanation(checks: tuple[str, ...]) -> bool:
    if not has_explanation(checks):
        return False
    selectors = {requirement.result_pointer for check in checks
                 if (requirement := parse_tool_success_check(check)) is not None
                 and requirement.tool == "browser_upload" and requirement.answer_pointer is not None}
    return selectors >= {"/name", "/sha256"}


def upload_completion_scope(session: TaskSession) -> UploadScope | None:
    scope = upload_scope(session)
    if scope is not None and (scope.report_hash or bound_upload_explanation(session.policy.required_checks)):
        return scope
    return None


def task_upload_scope(task_kind: str, task: str, urls: frozenset[str], policy: TaskPolicy) -> UploadScope | None:
    if task_kind != "chat" or policy.strict_research or len(urls) != 1 or not policy.allows("browser.effect"):
        return None
    task = re.sub(r"```[\s\S]*?```", "", task)
    actions = list(_UPLOAD.finditer(task))
    attachment_matches: list[str] = _ATTACHMENT.findall(task)
    attachments = set(attachment_matches)
    if not actions or any(_NEGATED.search(task[:match.start()]) for match in actions) or len(attachments) > 1:
        return None
    checks = policy.required_checks
    return UploadScope(next(iter(urls)), next(iter(attachments), ""),
        _NAVIGATE in checks or re.search(r"\bbrowser_navigate\b", task) is not None,
        _REPORT_HASH.search(task) is not None, _READ in checks or _PAGE_TEXT.search(task) is not None,
        bool(re.search(r"[\u3400-\u9fff]", task)), checks)


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
            elif isinstance(value := result.canonical.get("value"), dict) and decode_json_object(value).get("reason") == "file_observation_expired":
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
    read = _upload_read_spec(scope.wait_checks)
    options = {"observe": observe, "select": select, "read": read}[phase]
    return freeze_spec_parameters(replace(spec, parameters=(*spec.parameters, (UPLOAD_ACTION, options)), required=()))


def _upload_read_spec(checks: tuple[str, ...]) -> dict[str, object]:
    plan = upload_wait_plan(checks)
    properties: dict[str, object] = {"action": {"type": "string", "enum": ["read"]}, "wait_for_text": _LABEL}
    required = ["action"]
    if plan.missing:
        if plan.needs_arguments:
            wait_spec = spec_for_tool("browser_wait")
            assert wait_spec is not None
            properties["waits"] = {"type": "array", "minItems": 1, "maxItems": len(plan.missing),
                "items": _object(dict(wait_spec.parameters), *wait_spec.required),
                "description": "Supply executable waits covering these missing argument predicates: " + str(plan.missing)}
            required.append("waits")
        else:
            properties["wait_for_text"] = {**_LABEL, **(
                {"enum": [args["value"] for args in plan.missing if "value" in args]}
                if any("value" in args for args in plan.missing) else {})}
            required.append("wait_for_text")
    return _object(properties, *required)


def upload_model_spec(spec: ToolSpec) -> ToolSpec:
    parameters = tuple(row for row in spec.parameters if row[0] == UPLOAD_ACTION)
    if spec.name != "codemode" or not parameters:
        return spec
    return replace(spec, parameters=parameters, required=(UPLOAD_ACTION,), declarations="", json_examples=(),
        description="Structured attachment selection on the authorized webpage. Use the action offered in this turn's "
        "schema. Observe lists file controls. Add the authorized URL only when the page needs opening. Select accepts "
        "a unique name or an observed ID. If names repeat, choose an observed ID. Read executes declared task waits "
        "before reading. Supply wait_for_text or waits as required by the schema to cover missing task conditions.")


def lower_upload_action(args: JsonObject, checks: tuple[str, ...] = ()) -> JsonObject:
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
        supplied: list[JsonObject] = []
        extra = action.get("waits", [])
        if not isinstance(extra, list):
            raise ValueError("waits must be an array of browser_wait arguments")
        supplied.extend(decode_json_object(value) for value in extra)
        marker = action.get("wait_for_text")
        if isinstance(marker, str):
            bound = upload_wait_plan(checks).waits
            if not any(wait.get("condition") == "text" and wait.get("value") == marker for wait in bound):
                supplied.append({"condition": "text", "value": marker, "timeout_ms": 5000})
        plan = upload_wait_plan(checks, tuple(supplied))
        if plan.missing:
            raise ValueError("Upload read is missing required wait arguments: " + str(plan.missing))
        action["waits"] = [wait for wait in plan.waits]
        code = ('for (const wait of inputs.waits) unwrap(await tools.browser_wait(wait)); '
                'return unwrap(await tools.browser_read({}));')
    return {"code": code, "inputs": action}


def matches_document(document: object, url: str) -> bool:
    return (isinstance(document, dict) and isinstance(loader := document.get("loader_id"), str) and bool(loader)
            and isinstance(observed := document.get("url"), str) and source_url_key(observed) == url)


def browser_opened(result: ToolResult, url: str) -> bool:
    requested = result.call.args.get("url")
    return isinstance(requested, str) and source_url_key(requested) == url and browser_delivered_source(result, url)


def browser_delivered_source(result: ToolResult, url: str) -> bool:
    acquisition = result.canonical.get("source_acquisition")
    attempts = decode_json_object(acquisition).get("attempts") if isinstance(acquisition, dict) else None
    if not result.ok or not isinstance(attempts, list) or not attempts or not isinstance(last := attempts[-1], dict):
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
    selection_ref: str
    page_ref: str

    def status(self, *, chinese: bool) -> str:
        # browser_upload verifies input selection. A subsequent browser_read
        # observes the page and does not establish server acceptance.
        if chinese:
            page = "已读取选择后的页面。" if self.text else ""
            return f"已在网页选中附件。{page}服务器接收状态未核验。"
        page = " The page was read after selection." if self.text else ""
        return f"The attachment was selected on the page.{page} Server acceptance was not verified."

    def explanation_issue(self, explanation: str, *, chinese: bool) -> str:
        # Recognize explicit known claims, not arbitrary prose semantics or
        # quoted page content. Keep the draft intact for a completion retry.
        for clause in _STATUS_CLAUSES.split(_QUOTED_STATUS.sub(" ", explanation)):
            for claim in _UPLOAD_ASSERTION.finditer(clause):
                if (_NEGATED_ASSERTION.search(clause[:claim.start()])
                        or _UNVERIFIED_ASSERTION.search(clause[claim.end():])):
                    continue
                instruction = ("请只修正 done.summary 中没有回执支持的上传或服务器接收状态，保留其余分析。"
                               "重新提交 done，不要重复选择或上传附件。" if chinese else
                               "Correct only the unsupported upload or server-acceptance claim in done.summary; "
                               "preserve the remaining analysis. Resubmit done without selecting or uploading again.")
                return (self.status(chinese=chinese) + " " + instruction
                        + "\n\nRejected explanation to revise:\n" + explanation)
        return ""

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
    selection_ref = page_ref = ""
    for ref, result in committed_results(session.executed, session._memory_results, _SELECTION_RECEIPTS):
        if result.audit.get("browser_document") != document:
            continue
        if result.call.name == "browser_upload":
            raw_value = result.canonical.get("value")
            value = decode_json_object(raw_value) if isinstance(raw_value, dict) else None
            if (not result.truncated and isinstance(value, dict) and value.get("delivery") == "verified"
                    and (not scope.attachment or value.get("attachment_id") == scope.attachment)
                    and isinstance(name := value.get("name"), str) and name
                    and isinstance(digest := value.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", digest)):
                selection, body, selection_ref, page_ref = (name, digest), "", ref, ""
        elif result.call.name == "browser_read" and selection is not None:
            try:
                body = _browser_body(result) or ""
            except (KeyError, TypeError, ValueError):
                body = ""
            page_ref = ref if body else ""
    if selection is None:
        return "Select the requested attachment on the target page and obtain its successful upload receipt."
    if scope.read_page and not body:
        return "Read the complete target page after selecting the attachment. Reuse the existing upload receipt."
    return UploadEvidence(*selection, body if scope.read_page else "", selection_ref, page_ref)
