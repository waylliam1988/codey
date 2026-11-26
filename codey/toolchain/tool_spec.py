"""One tool registry for the unified kernel (operations-facing).

Coding definitions, Research contracts, and controller aliases converge here
as ``ToolSpec`` rows: one grant, one parameter schema, one canonical name.
Text batch wrappers lower to validated project calls before execution. Native
calls use individual ids; wrappers are never advertised there. Unknown tools
are denied, never passed as ``control``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from typing import Protocol

from codey.protocols.codemode import codemode_parameters
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import ARGUMENT_CONTENT_GUIDANCE, PAGE_DETAIL_TOOL
from codey.toolchain.result_schema import text_output_schema
from codey.toolchain.schema_validation import (
    _valid_custom_schema,
    check_schema_value,
)
from codey.toolchain.spec_model import EXECUTION_CLAIMS_GUIDANCE, ToolSpec, freeze_spec_parameters, schema_for_spec
from codey.toolchain.tool_names import MODEL_NAME_BY_RUNTIME_NAME
from codey.toolchain.tool_prompt import json_contract_text as json_contract_text
from codey.toolchain.tool_prompt import model_tool_description
from codey.types.json_value import ToolArgs, canonical_json_object, parse_json_object

_TEXT_BATCH_TOOLS = frozenset({"parallel", "read_files"})
SOURCE_GAPS_GUIDANCE = (
    "The runtime checks all execution receipts and required source coverage; neither execution_claims nor "
    "source_gaps needs to list them all. Optional source_gaps cites actual failed acquisition receipts. "
)


class ToolPolicy(Protocol):
    def allows(self, grant: str) -> bool: ...


CustomExecutor = Callable[[ToolCall], ToolResult]


def is_text_batch_tool(name: str) -> bool:
    """Wrappers need text-plan lowering; they are not callable tool leaves."""
    return name in _TEXT_BATCH_TOOLS

# Controller alias -> canonical research tool + required id arg.
_CONTROLLER_ALIAS_ID_ARG = {
    "open_result": ("open_url", "result_id"),
    "reopen_source": ("open_url", "source_id"),
    "open_hit": ("open_url", "hit_id"),
}

SOURCE_READ_TOOL_NAMES = frozenset({"open_url", *_CONTROLLER_ALIAS_ID_ARG})
SOURCE_TOOL_NAMES = SOURCE_READ_TOOL_NAMES | {"web_search", "source_search"}


def _coding_specs() -> dict[str, ToolSpec]:
    from codey.toolchain import definition as tool_defs

    specs: dict[str, ToolSpec] = {}
    grant_by_permission = {
        "project_read": "project.read",
        "project_write": "project.write",
        "project_verify": "project.verify",
        "user_approved_shell": "shell.approval",
        "control": "control",
    }
    for definition in tool_defs.TOOL_DEFINITIONS:
        grant = grant_by_permission.get(definition.permission, "")
        if not grant:
            continue
        specs[definition.name] = ToolSpec(
            name=definition.name,
            aliases=tuple(definition.aliases),
            grant=grant,
            parameters=tuple(definition.parameters),
            required=() if definition.name == "list_dir" else tuple(definition.required),
            json_examples=definition.examples,
            description=definition.description,
            executor="project",
            replay_class="safe" if definition.read_only else "unsafe",
            parallel_safe=definition.parallel_safe,
            supports_background=definition.supports_background,
            output_schema=definition.output_schema,
        )
    return specs


def _research_specs() -> dict[str, ToolSpec]:
    from codey.research.calculation_schema import calculation_output_schema, data_output_schema
    from codey.research.knowledge_schema import schema_object, schema_strings
    from codey.research.tool_declarations import TOOL_CONTRACTS
    from codey.types.json_value import canonical_json_object

    grant_by_tool = {
        "read_data": "control",
        "calculate": "control",
        "research_plan": "knowledge.read",
        "web_search": "web.read",
        "open_url": "web.read",
        "source_search": "web.read",
        "knowledge_search": "knowledge.read",
        "knowledge_read": "knowledge.read",
        "knowledge_write": "knowledge.write",
        "save_finding": "knowledge.write",
        "save_inference": "knowledge.write",
        "knowledge_commit": "knowledge.write",
        "knowledge_link": "knowledge.link",
        "submit_research_answer": "knowledge.write",
        "done": "control",
    }
    specs: dict[str, ToolSpec] = {}
    for name, grant in grant_by_tool.items():
        contract = TOOL_CONTRACTS.get(name)
        if contract is None:
            continue
        schema = canonical_json_object(contract.input_schema)
        specs[name] = ToolSpec(
            name=name,
            aliases=(),
            grant=grant,
            parameters=tuple(schema_object(schema, "properties").items()),
            required=tuple(schema_strings(schema, "required")),
            json_examples=(contract.example,) if contract.example else (),
            description=contract.description,
            executor="receipt" if name in {"read_data", "calculate"} else "source" if grant == "web.read" else "knowledge",
            replay_class="safe" if grant in {"knowledge.read", "control"} or name in {"knowledge_commit", "save_finding", "save_inference", "submit_research_answer"} else "unsafe",
            output_schema=data_output_schema() if name == "read_data" else
                calculation_output_schema() if name == "calculate" else text_output_schema(),
            parallel_safe=contract.parallel_safe,
            evidence_route="source_read" if name in SOURCE_READ_TOOL_NAMES else "none",
        )
    for alias, (_canonical, id_arg) in _CONTROLLER_ALIAS_ID_ARG.items():
        reference_help = {
            "open_result": "Open a web_search hit. result_id is r1, r2, etc. from Search result references; "
                           "never pass a tool result_ref or URL. For a URL use open_url(url).",
            "reopen_source": "Read the captured source without fetching again. source_id is s1, s2, etc. "
                             "from Opened source references. Use open_url at offset 0 to acquire a fresh version.",
            "open_hit": "Read the exact captured source version located by source_search without fetching again. "
                        "hit_id is the exact id returned by source_search.",
        }
        specs[alias] = ToolSpec(
            name=alias,
            aliases=(),
            grant="web.read",
            parameters=((id_arg, {"type": "string"}),),
            required=(id_arg,),
            json_examples=(f'{{"tool":"{alias}","args":{{"{id_arg}":"..."}}}}',),
            description=reference_help[alias],
            executor="source",
            replay_class="unsafe" if alias == "open_result" else "safe",
            evidence_route="source_read",
        )
    return specs


def _browser_specs() -> dict[str, ToolSpec]:
    from codey.toolchain.browser_native_specs import browser_native_specs
    from codey.toolchain.browser_read_specs import browser_read_specs
    """Canonical browser contracts; execution is supplied by the task runtime."""
    specs = {
        "browser_snapshot": ToolSpec(
            name="browser_snapshot", grant="browser.observe", executor="browser",
            namespace="browser.observe", effect="read",
            parameters=(("max_elements", {"type": "integer", "minimum": 1, "maximum": 80}),
                        ("max_text", {"type": "integer", "minimum": 500, "maximum": 12000}),
                        ("scope_ref", {"type": "string"}), ("selector", {"type": "string"}),
                        ("base_revision", {"type": "integer", "minimum": 1})),
            description="Observe the bound page as bounded text and named interactive element refs.",
            json_examples=('{"tool":"browser_snapshot","args":{}}',),
            preconditions=("browser_bound",),
        ),
        **browser_read_specs(),
        "browser_click": ToolSpec(
            name="browser_click", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="page_mutation",
            parameters=(("ref", {"type": "string"}), ("result_ref", {"type": "string"})), required=("ref",),
            description="Click an observed ref. Reobservation preserves the same actual node; detached or repurposed nodes are rejected.",
            json_examples=('{"tool":"browser_click","args":{"ref":"<snapshot ref>"}}',),
            preconditions=("browser_bound", "fresh_snapshot"),
        ),
        "browser_fill": ToolSpec(
            name="browser_fill", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="page_mutation",
            parameters=(("ref", {"type": "string"}), ("value", {"type": "string"})),
            required=("ref", "value"),
            description="Fill and verify a textbox ref from the latest browser snapshot.",
            json_examples=('{"tool":"browser_fill","args":{"ref":"<snapshot ref>","value":"..."}}',),
            preconditions=("browser_bound", "fresh_snapshot"),
        ),
        "browser_scroll": ToolSpec(
            name="browser_scroll", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="page_mutation",
            parameters=(("direction", {"type": "string", "enum": ["up", "down", "left", "right"]}),
                        ("amount", {"type": "integer", "minimum": 1, "maximum": 20000}), ("ref", {"type": "string"})),
            required=("direction", "amount"),
            description="Scroll the bound page by a bounded amount.",
            json_examples=('{"tool":"browser_scroll","args":{"direction":"down","amount":500}}',),
            preconditions=("browser_bound",),
        ),
        "browser_wait": ToolSpec(
            name="browser_wait", grant="browser.observe", executor="browser",
            supports_background=True,
            namespace="browser.observe", effect="read",
            parameters=(("condition", {"type": "string", "enum": ["text", "text_absent", "text_changed", "controls_changed", "selector", "selector_hidden", "selector_detached", "url_contains", "new_document", "popup", "dialog", "download"]}),
                        ("value", {"type": "string"}),
                        ("timeout_ms", {"type": "integer", "minimum": 100, "maximum": 30000})),
            required=("condition", "value"),
            description="Wait for one explicit bounded condition on the bound page. "
                        "For text_changed, value is a committed full-page browser_snapshot result_ref: "
                        "wait for its visible text to differ on the original document, then observe the page again. "
                        "For controls_changed, value is the original complete control snapshot result_ref; wait for "
                        "a new, renamed or newly enabled accessible control on that document. "
                        "This is observation readiness, not confirmation of a requested business result. "
                        "timeout_ms is optional: preserve an explicit value, otherwise use the matching current-stage "
                        "declared timeout, or 5000 ms. Invalid timeouts are rejected.",
            json_examples=('{"tool":"browser_wait","args":{"condition":"text","value":"...","timeout_ms":5000}}',),
            preconditions=("browser_bound",),
        ),
        "browser_navigate": ToolSpec(
            name="browser_navigate", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="navigation",
            parameters=(("url", {"type": "string", "minLength": 1}),), required=("url",),
            description="Navigate the current browser page to a user-authorized URL.",
            json_examples=('{"tool":"browser_navigate","args":{"url":"https://example.com"}}',),
        ),
        "browser_eval": ToolSpec(
            name="browser_eval", grant="browser.effect", executor="browser",
            namespace="browser.effect", effect="page_mutation",
            parameters=(("code", {"type": "string", "minLength": 1,
                                  "description": "Page JavaScript expression or function, e.g. document.title or () => document.title; not a function body with a bare return."}),), required=("code",),
            description="Evaluate page JavaScript only when the browser effect capability is authorized.",
            json_examples=('{"tool":"browser_eval","args":{"code":"document.title"}}',),
            preconditions=("browser_bound",),
        ),
        "browser_tab_list": ToolSpec(
            name="browser_tab_list", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="read",
            description="List pages in tabs; page_url identifies the currently bound browser page.",
            json_examples=('{"tool":"browser_tab_list","args":{}}',),
        ),
        "browser_tab_new": ToolSpec(
            name="browser_tab_new", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="navigation",
            parameters=(("url", {"type": "string", "minLength": 1}),), required=("url",),
            description="Open a new tab at a user-authorized URL.",
            json_examples=('{"tool":"browser_tab_new","args":{"url":"https://example.com"}}',),
        ),
        "browser_tab_switch": ToolSpec(
            name="browser_tab_switch", grant="browser.interact", executor="browser",
            namespace="browser.interact", effect="navigation",
            parameters=(("index", {"type": "integer", "minimum": 0}),), required=("index",),
            description="Switch to a tab index from the latest tab list.",
            json_examples=('{"tool":"browser_tab_switch","args":{"index":0}}',),
            preconditions=("browser_bound",),
        ),
        "browser_upload": ToolSpec(
            name="browser_upload", grant="browser.effect", executor="browser",
            namespace="browser.effect", effect="external_effect",
            parameters=(("ref", {"type": "string"}),
                        ("attachment_id", {"type": "string", "pattern": "^att_[a-f0-9]{64}$"})),
            required=("ref", "attachment_id"),
            description="Select a user-imported attachment from this chat in a file input. Returns verified selection metadata, not proof of server submission. Host filesystem paths are not accepted.",
            json_examples=('{"tool":"browser_upload","args":{"ref":"<snapshot ref>","attachment_id":"<user attachment id>"}}',),
            preconditions=("browser_bound", "fresh_snapshot", "explicit_file_authorization"),
        ),
        "browser_download": ToolSpec(
            name="browser_download", grant="browser.effect", executor="browser",
            namespace="browser.effect", effect="external_effect",
            parameters=(("ref", {"type": "string"}),), required=("ref",),
            description="Download through an authorized link ref. Return a managed path, byte count, SHA256 and artifact handle; the suggested filename never chooses a host path.",
            json_examples=('{"tool":"browser_download","args":{"ref":"<snapshot ref>"}}',),
            preconditions=("browser_bound", "fresh_snapshot", "managed_output_storage"),
        ),
    }
    from codey.toolchain.result_schema import browser_output_schema

    specs.update(browser_native_specs())

    return {name: replace(spec, output_schema=browser_output_schema(name)) for name, spec in specs.items()}


def _all_specs() -> dict[str, ToolSpec]:
    specs = _coding_specs()
    codemode_example = (
        "const result = await tools.read_file({path:inputs.path}); "
        "const file = unwrap(result); return file.text;"
    )
    codemode_inputs = {"path": "app.py"}
    data_example = {"code": "return inputs.text;", "inputs": {"text": "first\nsecond\n"}}
    code_example: dict[str, object] = {"tool": "codemode", "args": {
        "code": codemode_example, "inputs": codemode_inputs}}
    specs["codemode"] = ToolSpec(
        name="codemode",
        grant="control",
        executor="codemode",
        replay_class="safe",
        description=(
            "Write an async JavaScript function body using tools.* and return its JSON result explicitly. "
            "Use return null for an intentional empty result. Normal authorization applies. "
            "Returning modified data does not persist it; invoke the declared action tool to change external state. "
            "Calls return Result<T>; use unwrap(await tools.NAME(...)) to obtain T. "
            "Access only fields declared for that tool's output schema. "
            "Await all tool/store/load calls. console.log/error fail; only return delivers data. "
            "No direct filesystem, process, network, shell or recursive CodeMode access. "
            f"{ARGUMENT_CONTENT_GUIDANCE} "
            "Put task data (paths, search queries, file contents) in inputs, not JS string literals. "
            "Read it as inputs.name; inputs is immutable JSON data, never executable source. "
            f"Example inputs={json.dumps(codemode_inputs)}; JavaScript: {codemode_example}\n"
            f"Multiline data arguments: {json.dumps(data_example)}"
        ),
        parameters=codemode_parameters(),
        required=("code",),
        json_examples=(json.dumps(code_example, separators=(",", ":")),),
    )
    specs["ask_user"] = ToolSpec(
        name="ask_user", grant="control", replay_class="safe", executor="control",
        description="Ask a bounded choice question and pause for the user's answer. This authorizes no command. Must be the only call in the turn.",
        parameters=(("question", {"type": "string"}), ("options", {"type": "array", "items": {"type": "string"}}),
                    ("descriptions", {"type": "array", "items": {"type": "string"}}), ("allow_free_text", {"type": "boolean"})),
        required=("question", "options"),
        json_examples=('{"tool":"ask_user","args":{"question":"Which scope?","options":["Collect tests","Run tests"]}}',),
    )
    specs["read_tool_result"] = ToolSpec(
        name="read_tool_result", grant="control", executor="receipt", replay_class="safe",
        description="Read a stored result from this task without repeating its execution. "
                    "Use an owned tool_task reference for live status and output, or handoff:TOOL to list authorized original receipts by tool. "
                    "Use view=value for the full canonical JSON value, or view=output (default) for display/command output. "
                    "A missing canonical value is an error; display text cannot replace it. "
                    "Use query (a literal keyword, 1-512 characters) to locate a bounded excerpt in long output; "
                    "offset starts the search or selects a page. Not a regular expression.",
        parameters=(("result_ref", {"type": "string"}), ("view", {"type": "string", "enum": ["output", "value"]}),
                    ("offset", {"type": "integer", "minimum": 0}),
                    ("limit", {"type": "integer", "minimum": 1, "maximum": 8000}),
                    ("query", {"type": "string"})),
        required=("result_ref",),
        json_examples=('{"tool":"read_tool_result","args":{"result_ref":"...","query":"FAILED","limit":4000}}',),
    )
    for name, spec in _research_specs().items():
        if name == "done":
            # Ordinary completion carries knowledge citations, never a Research report.
            coding_done = specs.get("done")
            if coding_done is not None:
                specs["done"] = replace(coding_done, description=coding_done.description + " " + spec.description, parameters=(
                    *coding_done.parameters, ("knowledge_refs", dict(spec.parameters)["knowledge_refs"]),
                ))
            continue
        specs[name] = spec
    specs.update(_browser_specs())
    from codey.toolchain.task_control_specs import task_control_specs

    specs.update(task_control_specs())
    if "done" not in specs:
        specs["done"] = _coding_specs().get("done", ToolSpec(name="done", grant="control"))
    done = specs["done"]
    specs["done"] = replace(done, description=done.description +
        " " + EXECUTION_CLAIMS_GUIDANCE + SOURCE_GAPS_GUIDANCE +
        "Use the current source coverage to explain limitations in the requested final format.", parameters=(*done.parameters,
        ("execution_claims", {"type": "array", "maxItems": 64, "items": {"type": "object",
            "required": ["result_ref", "status", "exit_code_scope"], "additionalProperties": False,
            "properties": {"result_ref": {"type": "string"}, "status": {"type": "string"},
                "exit_code_scope": {"type": "string", "enum": ["shell", "process"]}, "exit_code": {"type": "integer"}}}}),
        ("source_gaps", {"type": "array", "minItems": 1, "maxItems": 16, "items": {"type": "object",
            "required": ["result_ref", "reason"], "additionalProperties": False,
            "properties": {"result_ref": {"type": "string"}, "reason": {"type": "string", "minLength": 1}}}}),
    ))
    return {name: _execution_parameters(spec) for name, spec in specs.items()}


def _execution_parameters(spec: ToolSpec) -> ToolSpec:
    if not spec.supports_background:
        return spec
    if "run_in_background" in dict(spec.parameters):
        raise ValueError("execution option conflicts with business parameters")
    example = parse_json_object(spec.json_examples[0])
    args = canonical_json_object(example["args"])
    args["run_in_background"] = True
    example["args"] = args
    background_example = json.dumps(example, ensure_ascii=False, separators=(",", ":"))
    return replace(spec, parameters=(*spec.parameters, ("run_in_background", {
        "type": "boolean", "description": "Omit to use this tool's normal waiting behavior. "
        "True returns immediately; false waits for completion. Final results are delivered automatically."})),
        description=spec.description + " Background work uses the original tool and arguments. "
        "Final status and bounded output are delivered automatically; do not poll or repeat the operation. "
        "Set run_in_background=true to return immediately, or false to wait for completion. "
        "Acceptance is not completion. Use task_output for useful intermediate output, or task_cancel to stop. "
        f"Background call example: {background_example}")


_SPECS: dict[str, ToolSpec] | None = None


def tool_specs() -> dict[str, ToolSpec]:
    global _SPECS
    if _SPECS is None:
        _SPECS = _all_specs()
    return _SPECS


def register_custom_tool(
    name: object,
    *,
    grant: object = "control",
    parameters: tuple[tuple[str, object], ...] = (),
    required: tuple[str, ...] = (),
    description: object = "",
    executor: object = "custom",
) -> bool:
    """Register a third-task tool without editing the kernel loop."""

    canonical = str(name or "").strip().lower()
    if not canonical or canonical in tool_specs():
        return False
    grant_text = str(grant or "").strip().lower()
    from codey.policies.capabilities import KNOWN_TASK_GRANTS

    if grant_text not in KNOWN_TASK_GRANTS:
        return False
    def valid_schema(schema: object) -> bool:
        return _valid_custom_schema(schema)
    for parameter_name, schema in parameters:
        if not (parameter_name or "").strip() or not valid_schema(schema):
            return False
    if any(not (item or "").strip() for item in required):
        return False
    example_args: dict[str, object] = {}
    for key, schema in parameters:
        if key not in required:
            continue
        kind = str(schema.get("type", "")) if isinstance(schema, Mapping) else ""
        empty_array: list[object] = []
        empty_object: dict[str, object] = {}
        example_args[key] = empty_array if kind == "array" else empty_object if kind == "object" else (
            0 if kind in {"integer", "number"} else False if kind == "boolean" else "")
    example_action: dict[str, object] = {"tool": canonical, "args": example_args}
    example = json.dumps(example_action, ensure_ascii=False)
    tool_specs()[canonical] = ToolSpec(
        name=canonical,
        aliases=(),
        grant=grant_text,
        parameters=tuple(parameters),
        required=tuple(required),
        json_examples=(example,),
        description=str(description or ""),
        executor=str(executor or "custom"),
        replay_class="unsafe",
    )
    return True


def canonical_tool_name(raw: object) -> str:
    name = str(raw or "").strip().lower()
    if not name:
        return ""
    specs = tool_specs()
    if name in specs:
        return name
    if name in MODEL_NAME_BY_RUNTIME_NAME:
        return MODEL_NAME_BY_RUNTIME_NAME[name]
    for spec in specs.values():
        if name in spec.aliases:
            return spec.name
    return ""


def spec_for_tool(name: object) -> ToolSpec | None:
    canonical = canonical_tool_name(name)
    if not canonical:
        return None
    return tool_specs().get(canonical)


def is_source_read_tool(name: object) -> bool:
    """Evidence semantics belong to the registry, independently of transport."""
    spec = spec_for_tool(name)
    return spec is not None and spec.evidence_route == "source_read"


def tool_requires_trusted_recovery(name: object) -> bool:
    """True when replay without verified provenance must fail closed.

    Unknown tools, empty names, registry errors, and any replay class other
    than ``safe`` all require trusted recovery. Safe reads (project reads,
    web/knowledge reads) may use the safe-replay path.
    """
    try:
        spec = spec_for_tool(name)
    except Exception:
        return True
    if spec is None:
        return True
    return (spec.replay_class or "unsafe").strip().lower() != "safe"


def _policy_allows(policy: ToolPolicy, grant: str) -> bool:
    try:
        return policy.allows(grant)
    except Exception:
        return False


def visible_tool_names(policy: ToolPolicy) -> tuple[str, ...]:
    return visible_tool_names_for_snapshot(policy, None)


def _is_research_tool(name: str) -> bool:
    try:
        spec = tool_specs().get((name or "").strip().lower())
    except Exception:
        return False
    return spec is not None and spec.executor in {"source", "knowledge"}


def visible_tool_names_for_snapshot(
    policy: ToolPolicy, controller_allowed: Collection[str] | None = None, *, native: bool = False,
) -> tuple[str, ...]:
    """Per-turn snapshot: policy ∩ current controller facts.

    The controller may only narrow research tools; it never removes project
    tools from a hybrid run. ``None`` means no controller narrowing.
    """
    allowed: set[str] | None = None
    if controller_allowed is not None:
        try:
            allowed = {str(n or "").strip().lower() for n in controller_allowed}
        except TypeError:
            allowed = None
    order = ("list_dir", "read_file", "read_files", "grep", "find_references", "parallel", "codemode", "edit", "run", "shell",
             "web_search", "open_url", "source_search", "knowledge_search", "knowledge_read",
             "research_plan", "save_finding", "save_inference", "knowledge_write", "knowledge_commit", "knowledge_link", "read_tool_result", "done")
    names: list[str] = []
    for name in order:
        if native and is_text_batch_tool(name):
            continue
        spec = tool_specs().get(name)
        if spec is None or not spec.grant:
            continue
        if not _policy_allows(policy, spec.grant):
            continue
        if name == "codemode" and not any(_policy_allows(policy, grant) for grant in (
            "project.read", "browser.observe", "browser.interact", "browser.effect",
        )):
            continue
        if allowed is not None and name != "done" and _is_research_tool(name) and name not in allowed:
            # Controller aliases lower to open_url; keep open_url visible when
            # any alias is allowed so the model can still reach the web.
            if name == "open_url" and bool({"open_url", "open_result", "reopen_source", "open_hit"} & allowed):
                pass
            else:
                continue
        names.append(name)
    # Controller aliases are model-visible names; expose them when allowed.
    for alias in _CONTROLLER_ALIAS_ID_ARG:
        spec = tool_specs().get(alias)
        if spec is None or alias in names:
            continue
        if _policy_allows(policy, spec.grant) and (allowed is None or alias in allowed):
            names.append(alias)
    # Registered third-task tools advertise alongside built-ins; unregistered
    # names stay denied.
    for name in sorted(tool_specs()):
        if name in names or name in order or name in _CONTROLLER_ALIAS_ID_ARG or name == PAGE_DETAIL_TOOL:
            continue
        spec = tool_specs()[name]
        if spec.grant and _policy_allows(policy, spec.grant):
            if allowed is not None and _is_research_tool(name) and name not in allowed:
                continue
            names.append(name)
    return tuple(names)


def native_tools_for_policy(policy: ToolPolicy) -> list[dict[str, object]]:
    return native_tools_for_snapshot(policy, None)


def native_tools_for_snapshot(
    policy: ToolPolicy, controller_allowed: Collection[str] | None = None,
) -> list[dict[str, object]]:
    """Native schemas for one turn's snapshot; same source as JSON contract."""
    tools: list[dict[str, object]] = []
    for name in visible_tool_names_for_snapshot(policy, controller_allowed, native=True):
        spec = tool_specs().get(name)
        if spec is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": model_tool_description(spec),
                "parameters": schema_for_spec(spec),
            },
        })
    def tool_name(item: dict[str, object]) -> str:
        function = item.get("function")
        return str(function.get("name") or "") if isinstance(function, dict) else ""

    tools.sort(key=tool_name)
    return tools


def _builtin_alias_allowed_keys(tool: str) -> set[str] | None:
    """Legacy alias sets for project tools; None means strict spec only."""
    name = (tool or "").strip().lower()
    if name in {"list_dir", "ls"}:
        return {"path", "cwd"}
    if name in {"read_file", "read"}:
        return {"path", "cwd", "offset", "limit"}
    if name in {"grep", "search"}:
        return {"query", "pattern", "path", "cwd", "offset", "limit", "literal", "case_sensitive",
                "include", "exclude", "before_context", "after_context"}
    if name in {"find_references", "references"}:
        return {"symbol", "name", "path", "cwd"}
    if name in {"run", "shell"}:
        return {"command", "cmd", "path", "cwd"}
    if name == "source_search":
        # Controller alias form: source_id + query without url.
        return {"source_id"}
    return None


def _builtin_required_satisfied(tool: str, required: tuple[str, ...], args: dict[str, object]) -> str:
    """Alias-aware required check for built-ins; "" when satisfied else missing key."""
    name = (tool or "").strip().lower()
    # Groups where any member satisfies the canonical requirement.
    groups: dict[str, tuple[str, ...]] = {
        "path": ("path", "cwd"),
        "query": ("query", "pattern"),
        "symbol": ("symbol", "name"),
        "command": ("command", "cmd"),
    }
    # source_search special: source_id + query satisfies without url.
    if name == "source_search" and "source_id" in args:
        required = tuple(key for key in required if key != "url")
    for key in required:
        candidates = groups.get(key, (key,))
        found = False
        for cand in candidates:
            if cand in args:
                value = args.get(cand)
                if value is None:
                    continue
                # String required must be non-blank (after strip when str).
                if isinstance(value, str) and not value.strip():
                    continue
                found = True
                break
        if not found:
            return key
    return ""


def _declared_map(spec: ToolSpec) -> tuple[dict[str, object], str, bool]:
    declared: dict[str, object] = {}
    try:
        for param_name, schema in (spec.parameters or ()):
            declared[str(param_name)] = schema
    except Exception:
        declared = {}
    try:
        executor = str(spec.executor or "")
    except Exception:
        executor = ""
    return declared, executor, executor in {"project", "source", "knowledge"}


def _validate_required_presence(spec: ToolSpec, declared: dict[str, object], is_builtin: bool, args: dict[str, object]) -> str:
    required_keys: list[str] = []
    for key in spec.required:
        schema = declared.get(key)
        if isinstance(args.get(key), str) and isinstance(schema, Mapping):
            bounds: Mapping[str, object] = schema
            if bounds.get("minLength") == 0:
                continue
        required_keys.append(key)
    required = tuple(required_keys)
    if is_builtin:
        missing_key = _builtin_required_satisfied(spec.name, required, args)
        if missing_key:
            return f"{spec.name} missing required arg '{missing_key}'"
        return ""
    for key in required:
        if key not in args:
            return f"{spec.name} missing required arg '{key}'"
        value = args.get(key)
        if value is None:
            return f"{spec.name} missing required arg '{key}'"
        if isinstance(declared.get(key), Mapping):
            raw_declared = declared.get(key)
            want = str(raw_declared.get("type", "") or "").lower() if isinstance(raw_declared, Mapping) else ""
            if want == "string" and not str(value or "").strip():
                return f"{spec.name} missing required arg '{key}'"
    return ""


def validate_args_against_spec(name: object, args: object) -> str:
    """Single authoritative parameter check from the ToolSpec.

    Returns "" when args satisfy the ToolSpec (required presence, no extra
    args, JSON types); otherwise a short error. Project path/command/URL
    safety stays in the legacy validators, but shape is decided here so JSON
    and native prompts, schemas, and validation cannot drift.
    """
    spec = spec_for_tool(name)
    if spec is None:
        return f"unknown tool: {name or '?'}"
    return validate_args_with_spec(spec, args)


def _validate_no_extra(spec: ToolSpec, declared: dict[str, object], is_builtin: bool, args: ToolArgs) -> str:
    if is_builtin:
        allowed = _builtin_alias_allowed_keys(spec.name)
        if allowed is None:
            allowed = set(declared.keys())
        else:
            allowed |= declared.keys()
        for key in args:
            if key not in allowed:
                return f"{spec.name} unexpected arg '{key}'"
        return ""
    for key in args:
        if key not in declared:
            return f"{spec.name} unexpected arg '{key}'"
    return ""


def _validate_types(spec: ToolSpec, declared: dict[str, object], is_builtin: bool, args: dict[str, object]) -> str:
    if not is_builtin:
        for key, value in args.items():
            skey = key
            schema = declared.get(skey)
            if schema is None:
                continue
            error = check_schema_value(spec.name, skey, schema, value, strict=True)
            if error:
                return _parameter_error(error, schema)
        return ""
    _alias_to_canonical = {
        "cwd": "path", "pattern": "query", "name": "symbol", "cmd": "command",
        "source_id": "url",
    }
    for key, value in args.items():
        skey = key
        schema = declared.get(skey)
        if schema is None and is_builtin:
            canonical = _alias_to_canonical.get(skey)
            if canonical is not None:
                schema = declared.get(canonical)
            if schema is None:
                continue
        if schema is None:
            continue
        if skey == "source_id":
            if not isinstance(value, str) or not value.strip():
                return f"{spec.name} missing required arg 'source_id'"
            continue
        error = check_schema_value(spec.name, skey, schema, value)
        if error:
            return _parameter_error(error, schema)
    return ""


def _parameter_error(error: str, schema: object) -> str:
    description = schema.get("description", "") if isinstance(schema, Mapping) else ""
    return error + ". " + description if isinstance(description, str) and description else error


def validate_args_with_spec(spec: ToolSpec | None, args: object) -> str:
    """用给定冻结 spec 校验（快照本轮定义，不读 live 注册表）。"""
    if spec is None:
        return "unknown tool"
    if not isinstance(args, dict):
        return f"{spec.name} args must be an object"
    declared, _, is_builtin = _declared_map(spec)
    if "args" not in declared and isinstance(args.get("args"), dict):
        return (
            f"{spec.name} received an undeclared args wrapper. "
            "The tool name is already selected; pass its fields directly as arguments, not inside args. "
            f"Required top-level fields: {', '.join(spec.required) or '(none)'}. "
            f"Declared fields: {', '.join(declared)}. No tool was executed."
        )
    error = _validate_required_presence(spec, declared, is_builtin, args)
    if error:
        return error
    error = _validate_no_extra(spec, declared, is_builtin, args)
    if error:
        return error
    return _validate_types(spec, declared, is_builtin, args)


_CUSTOM_EXECUTORS: dict[str, CustomExecutor] = {}


def register_custom_executor(name: object, fn: CustomExecutor) -> bool:
    """Register a production executor for a third-task tool (generic path)."""
    canonical = str(name or "").strip().lower()
    if not canonical or not callable(fn):
        return False
    try:
        spec = spec_for_tool(canonical)
    except Exception:
        spec = None
    if spec is None:
        return False
    _CUSTOM_EXECUTORS[canonical] = fn
    return True


def custom_executor_for(name: object) -> CustomExecutor | None:
    canonical = str(name or "").strip().lower()
    if not canonical:
        return None
    return _CUSTOM_EXECUTORS.get(canonical)


def unregister_custom_tool(name: object) -> bool:
    canonical = str(name or "").strip().lower()
    if not canonical:
        return False
    removed = False
    try:
        if canonical in tool_specs():
            # Never remove built-ins; only third-task registrations.
            builtin = _all_specs().get(canonical)
            if builtin is None:
                tool_specs().pop(canonical, None)
                removed = True
    except Exception:
        pass
    if canonical in _CUSTOM_EXECUTORS:
        _CUSTOM_EXECUTORS.pop(canonical, None)
        removed = True
    return removed


__all__ = [
    "SOURCE_READ_TOOL_NAMES",
    "SOURCE_TOOL_NAMES",
    "ToolSpec",
    "canonical_tool_name",
    "custom_executor_for",
    "freeze_spec_parameters",
    "json_contract_text",
    "native_tools_for_policy",
    "native_tools_for_snapshot",
    "register_custom_executor",
    "register_custom_tool",
    "spec_for_tool",
    "tool_requires_trusted_recovery",
    "tool_specs",
    "unregister_custom_tool",
    "validate_args_against_spec",
    "validate_args_with_spec",
    "visible_tool_names",
    "visible_tool_names_for_snapshot",
]
