"""Local-model acceptance for source-bound arithmetic and semantic rename."""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.policies.tool_requirement import require_tool_success
from codey.types.json_value import JsonObject, decode_json_object
from tools import research_query_delivery_live_gate as gate
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, append_json, production_source_digest, write_json
from tools.followup_research_live import BIRCH, CASE, CEDAR, _read_refs


class Options(argparse.Namespace):
    directory: Path
    mode: str
    base_url: str


def calculation(directory: Path, base_url: str) -> bool:
    case = gate.QueryCase("/calculation-input", (
        "The same Atlas cohort used 24 kg at baseline and 31 kg at follow-up. Calculate its growth rate. "
        "Use a model-authored CodeMode program: read the page with open_url, then use read_data with the offered "
        "evidence label and format quantities. In JavaScript select the baseline and follow-up quantity entries "
        "from their context; do not retype numeric inputs. Call tools.calculate with growth_rate, operands as "
        "{result_ref: parsed.result_ref, pointer: '/data/<selected index>/value'}, scale 6. Keep the outer Result "
        "to obtain result_ref, use unwrap for its value. Source reading and data parsing may be separate turns "
        "so the frozen evidence labels become available. Submit a research report using the offered {{calc:cN}} "
        "slot whose operation is growth_rate for the computed growth rate, explain the denominator and the "
        "same-cohort assumption, and finish. Do not recalculate or retype the formula in the report."
    ), "<h1>Atlas same cohort</h1><p>Baseline fuel: 24 kg.</p><p>Follow-up fuel: 31 kg.</p>", ("29.166667",),
        required_tools=(require_tool_success("calculate", arguments={"operation": "growth_rate"}, via="codemode"),))
    outcome = gate.run_case(directory, case, select_target(240, base_url), max_turns=12, source_access_wording=True)
    receipts = tool_receipts(directory / "state")
    calculated = [row.result for row in receipts if row.result.ok and row.result.call.name == "calculate"]
    submitted = [row.result for row in receipts if row.result.ok and row.result.call.name == "submit_research_answer"]
    slots = any("{{calc:c" in str(row.call.args) and "29.166667" in str(row.canonical) for row in submitted)
    passed = outcome["passed"] is True and bool(calculated) and slots
    write_json(directory / "acceptance.json", {"passed": passed, "report_slots": slots,
        "calculations": [row.canonical for row in calculated], "outcome": outcome})
    return passed


def rename(directory: Path, base_url: str) -> bool:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    before_files = {"math.ts": "export function total(value: number): number { return value * 2; }\r\n",
        "app.ts": "import { total } from './math.js';\r\nexport const answer = total(3);\r\n"}
    for name, text in before_files.items():
        (project / name).write_bytes(text.encode())
    (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true,"module":"NodeNext","noEmit":true},"files":["math.ts","app.ts"]}', encoding="utf-8")
    tsc = shutil.which("tsc")
    if tsc is None:
        raise RuntimeError("Install TypeScript and its language server outside the fixture project")
    (project / "package.json").write_text(json.dumps({"type": "module", "scripts": {"test": f'"{tsc}" --project tsconfig.json'}}), encoding="utf-8")
    task = (
        "Rename the TypeScript function total to doubled across math.ts and app.ts, preserving all other bytes. "
        "First query syntax_query for function_declaration in math.ts; use semantic_definition operations references "
        "and hover on total at math.ts line 1. Then in your own CodeMode program call semantic_definition with "
        "operation rename, path math.ts, line 1, symbol total, new_name doubled. Keep the outer Result. Apply its "
        "read-only plan through tools.edit({semantic_result_ref: plan.result_ref}); unwrap the edit result and return it. "
        "Do not manually copy or re-create source replacements. Run the offered npm test verification candidate on "
        "the final files, inspect workspace_diff, then finish with done."
    )
    target = select_target(240, base_url)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    before = production_source_digest()
    write_json(directory / "request.json", {"task": task, "source": before, "model": target.model, "files": before_files})

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        return provider

    def observe(event: dict[str, object]) -> None:
        append_json(directory / "events.jsonl", event)

    try:
        with (patch("codey.app.provider_services.provider_availability", return_value={"local": True}),
              patch("codey.app.provider_services.provider_tab_availability", return_value={}),
              patch("codey.app.review_service.run_review", return_value=None)):
            result = run_headless(HeadlessRequest(project, task, provider_id="local", max_turns=15, allow_web=False,
                state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
                required_tools=(require_tool_success("syntax_query"), require_tool_success("edit", via="codemode"))),
                emit_jsonl=observe, connect_provider=connect)
    finally:
        provider.close()
    receipts = tool_receipts(directory / "state")
    final = {name: (project / name).read_bytes().decode() for name in before_files}
    checked = any(row.result.ok and row.result.call.name == "run" and row.exit_code == 0
                  and row.result.call.args.get("command") == "npm test" for row in receipts)
    planned = any(row.result.ok and row.result.call.name == "semantic_definition"
                  and isinstance(plan := row.result.canonical.get("rename_plan"), list) and len(plan) == 2 for row in receipts)
    semantic_program = any(row.result.call.name == "codemode" and "semantic_result_ref" in str(row.result.call.args.get("code"))
                           for row in receipts)
    stable = before == production_source_digest()
    passed = (result.stop_reason == "done" and stable and checked and planned and semantic_program
              and final == {name: text.replace("total", "doubled") for name, text in before_files.items()})
    write_json(directory / "receipts.json", [{"id": row.identity, "tool": row.result.call.name,
        "args": row.result.call.args, "ok": row.result.ok, "text": row.result.model_text,
        "canonical": row.result.canonical} for row in receipts])
    summary: JsonObject = {"passed": passed, "stop_reason": result.stop_reason, "checked": checked,
        "two_file_plan": planned, "semantic_program": semantic_program, "source_stable": stable, "files": decode_json_object(final),
        "failures": [{"tool": row.result.call.name, "text": row.result.model_text} for row in receipts if not row.result.ok]}
    write_json(directory / "acceptance.json", decode_json_object(summary))
    return passed


def findings(directory: Path, base_url: str) -> bool:
    case = replace(CASE, question=(
        "Before any save_finding call, finish reading BOTH the Cedar and Birch sources. "
        "Cedar supports ONLY lower; Birch supports ONLY higher. Do not save the higher comparison from Cedar. "
    ) + CASE.question + (
        " Bind the lower comparison ONLY to the Cedar passage and the higher comparison ONLY to the Birch passage. "
        "Use the actual passage_ref from each source's own original read; the two findings must select different "
        "passages. After saving the two declared comparisons, submit the report without additional finding saves."))
    with patch.object(ResumableBrowserSearchProvider, "search", return_value=[{"title": "Cedar benchmark", "url": CEDAR,
            "snippet": "Atlas fuel consumption benchmark"}]):
        outcome = gate.run_case(directory, case, select_target(240, base_url), max_turns=12, source_access_wording=True)
    receipts = tool_receipts(directory / "state")
    refs = _read_refs(directory)
    saved = [row.result for row in receipts if row.result.ok and row.result.call.name == "save_finding"]
    unscoped = sum(not isinstance(row.call.args.get("comparison"), dict) for row in saved)
    paired = len(saved) == 2 and not unscoped and all(row.call.args.get("passage_ref") == refs.get(
        CEDAR if decode_json_object(row.call.args.get("comparison")).get("direction") == "lower" else BIRCH) for row in saved)
    searches = sum(row.result.ok and row.result.call.name == "web_search" for row in receipts)
    passed = outcome["passed"] is True and paired and not unscoped and searches == 1
    write_json(directory / "acceptance.json", {"passed": passed, "paired": paired, "unscoped_findings": unscoped,
        "saved": len(saved), "searches": searches, "outcome": outcome})
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--mode", choices=("calculation", "rename", "findings"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    options = parser.parse_args(namespace=Options())
    runner = {"calculation": calculation, "rename": rename, "findings": findings}[options.mode]
    return 0 if runner(options.directory.resolve(), options.base_url) else 1


if __name__ == "__main__":
    raise SystemExit(main())
