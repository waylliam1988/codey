"""Independent browser entry experiments over the original CodeMode host."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.codemode import catalog as catalog_module
from codey.codemode.host import CodeModeHost
from codey.codemode.runtime import NodeWorkerRuntime, ToolInvoker
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry, ToolCatalogSnapshot
from codey.types.json_value import JsonObject, JsonValue
from tools.completion_explanation_ab import append_json
from tools.structured_surface import prechecked_invoke

TARGETS = "browser-targets"
RESULTS = "browser-results"
PROPERTIES = "browser-properties"
WAIT = "browser-wait"
COMBINED = "browser-entry-combined"
FEATURES = frozenset({TARGETS, RESULTS, PROPERTIES, WAIT})
SCENARIOS = {
    TARGETS: ("basic", "stale_ref", "upload"),
    RESULTS: ("basic", "upload_bound", "download"),
    PROPERTIES: ("structured", "legacy_structured"),
    WAIT: ("basic", "snapshot_restore", "stale_ref"),
}
TARGET_ACTIONS = ("fill", "click", "upload", "download")
SCRIPT = Path(__file__).with_name("browser_entry_surface.js").read_text(encoding="utf-8")


def entry_methods(names: set[str], features: frozenset[str]) -> dict[str, str]:
    methods = {}
    if RESULTS in features:
        methods.update({name.removeprefix("browser_"): name for name in sorted(names)
                        if name.startswith("browser_") and name != "browser_eval"})
    if TARGETS in features and "browser_snapshot" in names:
        methods.update({name: "browser_" + name for name in TARGET_ACTIONS if "browser_" + name in names})
    if PROPERTIES in features:
        if "browser_tab_list" in names:
            methods["url"] = "browser_tab_list"
        if "browser_eval" in names:
            methods["property"] = "browser_eval"
    return methods


def declarations(rendered: str, entries: Sequence[ToolCatalogEntry], features: frozenset[str]) -> str:
    methods = entry_methods({entry.name for entry in entries}, features)
    if not methods:
        return rendered
    lines = ["// Use browser for the declared operations below. Original tools.* remains available.",
             "declare const browser: {"]
    for name, tool in methods.items():
        args = f"Parameters<typeof tools.{tool}>[0]"
        data = f"ReturnType<typeof tools.{tool}>"
        projection = f"BrowserData<typeof tools.{tool}>"
        if name in TARGET_ACTIONS and TARGETS in features:
            extra = {"fill": "; value:string", "upload": "; attachment_id:string"}.get(name, "")
            args = "{target_name:string; role?:string" + extra + "}"
            lines.append("  // target_name is the accessible control name. attachment_id identifies the selected file.")
        if name == "read":
            projection = "{text:string}"
        if name in {"url", "property"}:
            args = "{}" if name == "url" else "{name:string}"
            projection = "{value:unknown; type:string; truncated?:boolean}"
            data = f"Result<{projection}>"
        if RESULTS in features:
            data = f"Promise<BrowserEntry<{projection}>>"
        lines.append(f"  {name}(args: {args}): {data};")
    lines.append("};")
    if RESULTS in features:
        lines[:0] = [
            "type BrowserData<F> = F extends (...args: never[]) => Result<infer T> ? T : never;",
            "type BrowserEntry<T> = {status:'succeeded'; data:T; receipt_ref?:string; available_views:string[]} "
            "| {status:'failed'; error:string; snapshot?:unknown};",
            "// Use result.data directly after checking status. Return the result to deliver it.",
        ]
    return rendered + "\n" + "\n".join(lines)


def wrapped_code(code: str, catalog: Sequence[ToolCatalogEntry], features: frozenset[str]) -> str:
    methods = entry_methods({entry.name for entry in catalog}, features)
    if not methods:
        return code
    options = {"methods": methods, "targets": TARGETS in features, "results": RESULTS in features}
    return (f"return await (async (hostTools, options) => {{\n{SCRIPT}\nreturn await (async () => {{\n{code}\n}})();\n"
            f"}})(tools, {json.dumps(options, sort_keys=True)});")


@contextmanager
def prototype(directory: Path, features: frozenset[str]) -> Iterator[None]:
    if not features or not features <= FEATURES:
        raise ValueError("expected a nonempty set of browser entry candidates")
    render = catalog_module.render_declarations
    execute = NodeWorkerRuntime.execute
    digest = NodeWorkerRuntime.contract_digest
    invoke = CodeModeHost.invoke

    def render_entry(snapshot: ToolCatalogSnapshot) -> str:
        return declarations(render(snapshot), snapshot.entries, features)

    def checked(host: CodeModeHost, name: str, args: dict[str, object]) -> object:
        return prechecked_invoke(host, name, args, invoke) if name == "browser_wait" else invoke(host, name, args)

    def execute_entry(runtime: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry], invoke: ToolInvoker,
                      inputs: Mapping[str, object] | None = None, state_store: object = None,
                      replay_context: Mapping[str, object] | None = None, limits: CodeModeLimits | None = None) -> CodeModeResult:
        file_actions: list[JsonValue] = []

        def collect(name: str, args: dict[str, object]) -> object:
            result = invoke(name, args)
            if (RESULTS in features and name in {"browser_upload", "browser_download"}
                    and isinstance(result, dict) and result.get("ok") is True and result.get("result_ref")):
                row: JsonObject = {"tool": name, "receipt_ref": str(result["result_ref"])}
                file_actions.append(row)
            return result

        result = execute(runtime, wrapped_code(code, catalog, features), catalog=catalog, invoke=collect, inputs=inputs,
                         state_store=state_store, replay_context=replay_context, limits=limits)
        append_json(directory / "browser-entry-programs.jsonl", {"code": code, "ok": result.ok,
                    "error_code": result.error_code, "successful_file_actions": file_actions})
        if result.ok or not file_actions:
            return result
        return replace(result, error=result.error + "\nSuccessful file actions already settled: "
                       + json.dumps(file_actions) + ". Inspect their receipts before taking another file action.")

    def contract_digest(runtime: NodeWorkerRuntime) -> str:
        return hashlib.sha256((digest(runtime) + SCRIPT + "|".join(sorted(features))).encode()).hexdigest()

    with ExitStack() as stack:
        stack.enter_context(patch.object(NodeWorkerRuntime, "contract_digest", contract_digest))
        if WAIT in features:
            stack.enter_context(patch.object(CodeModeHost, "invoke", checked))
        if features - {WAIT}:
            stack.enter_context(patch.object(catalog_module, "render_declarations", render_entry))
            stack.enter_context(patch.object(NodeWorkerRuntime, "execute", execute_entry))
        yield
