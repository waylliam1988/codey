"""Exercise the multi-file patch contract with the configured local model."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.toolchain import runtime
from codey.toolchain.runtime import ToolOutcome
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, append_json, production_source_digest, write_json

TASK = (
    "In this project, change SERVICE_LIMIT in service.py from 12 to 37, and change the label in settings.ts "
    "from 'draft' to 'ready'. First use semantic_definition for SERVICE_LIMIT on line 2 of example.py. "
    "Make a two-file patch using the CodeMode tools.edit({files: ...}) overload. "
    "Submit one codemode call before done. Inside that program, read both files with tools.read_file, then return "
    "unwrap(await tools.edit({files: [firstFile, secondFile]})). Each file object takes path and replacements with "
    "old_string and new_string. The host binds the reads by path. A direct edit call fails this evaluation. "
    "Replace only the literal 12 with 37 and draft with ready. Preserve Python syntax, the TypeScript export, and surrounding bytes. "
    "After both edits, run the offered verification candidates for python -m pytest and npm test at the project root. "
    "Use workspace_diff to review the task's changes. "
    "Finish via done with a short report."
)


class Options(argparse.Namespace):
    directory: Path
    base_url: str
    inject_conflict: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--inject-conflict", action="store_true", help="Change the second file immediately before its version check")
    options = parser.parse_args(namespace=Options())
    directory = options.directory.resolve()
    project = directory / "project"
    project.mkdir(parents=True, exist_ok=False)
    files = {"service.py": "SERVICE_LIMIT = 12\n", "settings.ts": "export const label = 'draft';\n"}
    for path, content in files.items():
        (project / path).write_bytes(content.encode())
    (project / "example.py").write_text("from service import SERVICE_LIMIT\nprint(SERVICE_LIMIT)\n", encoding="utf-8")
    (project / "test_changes.py").write_text("from service import SERVICE_LIMIT\n\ndef test_limit():\n    assert SERVICE_LIMIT == 37\n", encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "package.json").write_text('{"type":"module","scripts":{"test":"node verify.mjs"}}', encoding="utf-8")
    (project / "verify.mjs").write_text("import {strictEqual} from 'node:assert';\nimport {label} from './settings.ts';\nstrictEqual(label, 'ready');\n", encoding="utf-8")
    target = select_target(120, options.base_url)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    state = directory / "state"
    before = production_source_digest()
    task = TASK + (" If a patch partially fails, reread the remaining file and continue only its missing edit. "
                   "Preserve any external comment; do not repeat completed writes." if options.inject_conflict else "")
    write_json(directory / "request.json", {"task": task, "model": target.model, "source": before, "files": files,
                                           "inject_conflict": options.inject_conflict})
    injected = False
    external = "// externally added; preserve this line\n"
    original_write = runtime._write_text_after_final_path_check

    def write(root: Path, rel: str, content: str, *, tool: str, expected_version: str | None = None) -> ToolOutcome | None:
        nonlocal injected
        if (options.inject_conflict and not injected and expected_version is not None
                and (root / rel).resolve() == project / "settings.ts"):
            injected = True
            path = project / "settings.ts"
            path.write_bytes(path.read_bytes() + external.encode())
            write_json(directory / "conflict-injection.json", {"path": rel, "expected_version": expected_version,
                                                               "external_bytes": external})
        return original_write(root, rel, content, tool=tool, expected_version=expected_version)

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        return provider

    def observe(event: dict[str, object]) -> None:
        append_json(directory / "events.jsonl", event)

    try:
        with (patch("codey.app.provider_services.provider_availability", return_value={"local": True}),
              patch("codey.app.provider_services.provider_tab_availability", return_value={}),
              patch.object(runtime, "_write_text_after_final_path_check", write)):
            result = run_headless(HeadlessRequest(project, task, provider_id="local", max_turns=20,
                allow_web=False, state_home=state, model_selection={"model": target.model, "base_url": target.base_url}),
                emit_jsonl=observe, connect_provider=connect)
    finally:
        provider.close()
    receipts = tool_receipts(state)
    write_json(directory / "receipts.json", [{"id": row.identity, "tool": row.result.call.name,
        "args": row.result.call.args, "ok": row.result.ok, "text": row.result.model_text,
        "audit": row.result.audit, "canonical": row.result.canonical} for row in receipts])
    final = {path: (project / path).read_text(encoding="utf-8") for path in files}
    edited = {str(row.result.call.args["path"]) for row in receipts if row.result.call.name == "edit"
              and isinstance(row.result.call.args.get("expected_version"), str)}
    writes = [row.result.call.args["path"] for row in receipts if row.result.call.name == "edit" and row.result.ok
              and row.result.audit.get("changed") is True]
    failures = [{"id": row.identity, "tool": row.result.call.name, "args": row.result.call.args,
                 "text": row.result.model_text} for row in receipts if not row.result.ok]
    stable = before == production_source_digest()
    diff = any(row.result.call.name == "workspace_diff" and row.result.ok for row in receipts)
    definition = any(row.result.call.name == "semantic_definition" and row.result.ok
                     and '"path": "service.py"' in row.result.model_text for row in receipts)
    checks = {str(row.result.call.args.get("command")) for row in receipts if row.result.call.name == "run"
              and row.result.ok and row.exit_code == 0}
    expected = {"service.py": "SERVICE_LIMIT = 37\n", "settings.ts": "export const label = 'ready';\n" + (external if injected else "")}
    passed = (stable and diff and definition and {"python -m pytest", "npm test"} <= checks
              and result.stop_reason == "done" and edited == set(files)
              and final == expected and writes.count("service.py") == writes.count("settings.ts") == 1
              and (not options.inject_conflict or injected and bool(failures)))
    write_json(directory / "summary.json", {"passed": passed, "stop_reason": result.stop_reason, "files": final,
        "preflighted_edits": sorted(edited), "workspace_diff": diff, "definition": definition,
        "checks": sorted(checks), "source_stable": stable, "conflict_injected": injected,
        "writes": writes, "failures": failures})
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
