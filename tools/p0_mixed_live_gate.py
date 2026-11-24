"""Run the mixed task against one local model with production receipts."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from codey.research.ledger_receipts import source_observation
from codey.research.passages import read_passage_refs
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object
from tools.browser_production_live_gate import HTML, URL, run_case, select_target
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import Target

DOC_URL = "https://example.com/production-record-format"
DOC = "<article><h1>Record format</h1><p>Amounts use integer grams and the code is a string.</p></article>"
PYTHON_CHECK = "python -m pytest"
NODE_CHECK = "npm test"
FILES = {
    "service/core.py": "AMOUNT = 0\n",
    "service/test_core.py": "from core import AMOUNT\n\ndef test_amount():\n    assert AMOUNT == 173\n",
    "service/pytest.ini": "[pytest]\n",
    "web/component.ts": 'export const code: string = "unknown";\n',
    "web/test.mjs": 'import {strictEqual} from "node:assert";\nimport {code} from "./component.ts";\nstrictEqual(code, "QUARTZ_851");\n',
    "package.json": '{"type":"module","scripts":{"test":"node web/test.mjs"}}',
}

NATURAL_VIEW_URL = "https://example.com/atlas/current-application"
NATURAL_FORMAT_URL = "https://example.com/atlas/record-specification"
NATURAL_FORMAT = ("<h1>Atlas record specification</h1><p>The stored JSON has exactly two fields, amount and code. "
                  "Amount is an integer number of grams. The record code is a case-sensitive string. "
                  "Locality selects the source record and is not stored in the JSON.</p>")
NATURAL_FILES = {
    **FILES,
    "service/test_core.py": FILES["service/test_core.py"].replace("173", "347"),
    "web/test.mjs": FILES["web/test.mjs"].replace("QUARTZ_851", "CEDAR_947"),
    "AGENTS.md": (
        "# Atlas workspace rules\n\n"
        "Read README.md for the record specification and the updated application page. "
        "Read the linked record specification before changing stored values.\n"
        "Keep the existing amount and code fields. The stored JSON record, Python service, "
        "and web adapter must agree. Preserve the existing test files. Verify the running "
        "application page after the edits and report what it actually displays.\n"
    ),
    "README.md": (
        "# Atlas record application\n\n"
        "The saved fishball record lives in result.json. The Python service exposes its amount "
        "as AMOUNT in service/core.py. The web adapter exposes its code in web/component.ts.\n"
        "The saved values and both public values must agree.\n"
        "Record JSON fields in `result.json`: `amount`, `code`\n"
        "Record interface: `result.json`\n"
        "Record interface: `service/core.py`\n"
        "Record interface: `web/component.ts`\n"
        f"Record specification: {NATURAL_FORMAT_URL}\n"
        f"Application browser page: {NATURAL_VIEW_URL}\n"
        "The existing project tests are under service/ and web/.\n"
    ),
}
_BUSINESS_FILES = ("result.json", "service/core.py", "web/component.ts")


@dataclass(frozen=True)
class NaturalMixedOracle:
    amount: int = 347
    code: str = "CEDAR_947"
    record_url: str = URL
    specification_url: str = NATURAL_FORMAT_URL
    application_url: str = NATURAL_VIEW_URL


_NATURAL_ORACLE = NaturalMixedOracle()


def _application_version(project: Path) -> str:
    return hashlib.sha256(b"\0".join((project / path).read_bytes() for path in _BUSINESS_FILES)).hexdigest()[:16]


def _verified_application_page(project: Path) -> str:
    record = parse_json_object((project / "result.json").read_text(encoding="utf-8"))
    return ("<h1>Atlas application record</h1>"
            f"<p>Build {_application_version(project)}.</p>"
            f"<p>Mass: {html.escape(str(record.get('amount')))} g.</p>"
            f"<p>Code: {html.escape(str(record.get('code')))}.</p>")


def _source_text(row: EffectReceipt) -> tuple[str, str]:
    receipt = row.result
    if not receipt.ok or not receipt.canonical.get("read_passages"):
        return "", ""
    try:
        source, _, _ = source_observation(receipt.canonical.get("research_observation"))
    except ValueError:
        return "", ""
    return source.final_url, receipt.model_text


def _hidden_checks(project: Path) -> JsonObject:
    commands = (
        ("python", [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"], project / "service"),
        ("node", ["node", "--test", "web/test.mjs"], project),
    )
    results: JsonObject = {}
    for name, args, cwd in commands:
        proc = cancellation.run_process(args, cwd=cwd, timeout=40, capture_limit_bytes=16000)
        results[name] = {"exit": proc.returncode, "output": proc.stdout + proc.stderr}
    return results


def _first_request(directory: Path) -> JsonObject:
    wire = directory / "provider.jsonl"
    if not wire.is_file():
        return {}
    for line in wire.read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("type") == "wire_attempt" and row.get("phase") == "request":
            return decode_json_object(row["payload"])
    return {}


def score_natural_mixed(project: Path, directory: Path, stop_reason: str,
                        oracle: NaturalMixedOracle = _NATURAL_ORACLE) -> JsonObject:
    receipts = tool_receipts(directory / "state")
    edited = [index for index, row in enumerate(receipts)
              if row.result.call.name == "edit" and row.result.ok and row.result.audit.get("changed") is True]
    protected = all((project / name).is_file() and (project / name).read_text(encoding="utf-8") == text
                    for name, text in NATURAL_FILES.items() if name not in _BUSINESS_FILES)
    record = parse_json_object((project / "result.json").read_text(encoding="utf-8"))
    values_correct = record == {"amount": oracle.amount, "code": oracle.code}
    tests = _hidden_checks(project) if protected else {}
    checks_passed = bool(tests) and all(decode_json_object(row)["exit"] == 0 for row in tests.values())
    sources: dict[str, EffectReceipt] = {}
    page: EffectReceipt | None = None
    build = _application_version(project)
    events = [parse_json_object(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    answer = next((str(row.get("summary", "")) for row in reversed(events) if row.get("type") == "task_done"), "")
    for index, row in enumerate(receipts):
        url, body = _source_text(row)
        if edited and index < edited[0]:
            if url == oracle.specification_url and "integer" in body and "grams" in body and "string" in body:
                sources[url] = row
            if url == oracle.record_url and str(oracle.amount) in body and oracle.code in body:
                sources[url] = row
        if (edited and index > edited[-1] and url == oracle.application_url and build in body
                and f"{oracle.amount} g" in body and oracle.code in body):
            page = row
    citations = {oracle.specification_url, oracle.record_url}
    cited = citations == set(sources) and all(url in answer for url in citations)
    first = _first_request(directory)
    initial_messages = json.dumps(first.get("messages", []), ensure_ascii=False)
    initial_clean = bool(first) and not re.search(rf"\b{oracle.amount}\b", initial_messages) and all(
        token not in initial_messages for token in (oracle.code, "#city", "#record", "pytest -q -p no:cacheprovider"))
    written = {path.replace("\\", "/").removeprefix("./") for index in edited
               if isinstance(path := receipts[index].result.call.args.get("path"), str)}
    after_edits = receipts[edited[-1] + 1:] if edited else ()
    current_checks = {(command, path or ".") for row in after_edits
        if row.result.call.name == "run" and row.result.ok and row.exit_code == 0
        and isinstance(command := row.result.call.args.get("command"), str)
        and isinstance(path := row.result.call.args.get("path", ""), str)}
    expected_checks = {(PYTHON_CHECK, "service"), (NODE_CHECK, ".")}
    dispatched_clicks = [row.identity for row in receipts if row.result.call.name == "browser_click" and row.result.ok]
    source_refs: JsonObject = {url: row.identity for url, row in sources.items()}
    source_passages: JsonObject = {url: list[JsonValue](read_passage_refs(row.result.canonical["read_passages"]))
        for url, row in sources.items()}
    before_edits = {row.identity: row.result for row in receipts if row.result.call.name == "read_file" and row.result.ok}
    edit_refs: list[JsonValue] = []
    versions_bound = bool(edited)
    for index in edited:
        row = receipts[index]
        basis = row.result.canonical.get("project_source_basis")
        if not isinstance(basis, dict):
            versions_bound = False
            continue
        binding = decode_json_object(basis)
        edit_refs.append({"result_ref": row.identity, "basis": binding})
        base = binding.get("base_result_ref")
        read = before_edits.get(base) if isinstance(base, str) else None
        versions_bound = (versions_bound and read is not None and binding.get("path") == read.call.args.get("path")
                          and binding.get("file_version") == read.canonical.get("file_version")
                          and binding.get("source_result_ref") == source_refs.get(oracle.record_url))
    checks = {"initial_payload_clean": initial_clean, "protected_files": protected, "business_values": values_correct,
              "requested_files_written": set(_BUSINESS_FILES) <= written, "independent_checks": checks_passed,
              "model_verified": expected_checks <= current_checks, "page_state_verified": page is not None,
              "bound_file_versions": versions_bound, "single_business_click": len(dispatched_clicks) == 1,
              "cited_sources": cited,
              "task_completed": stop_reason == "done"}
    first_missing = next((name for name, okay in checks.items() if not okay), "")
    return {"passed": not first_missing, "checks": {key: value for key, value in checks.items()},
            "first_missing": first_missing, "build": build, "page_ref": page.identity if page is not None else "",
            "source_refs": source_refs, "source_passages": source_passages, "edit_refs": edit_refs,
            "successful_browser_clicks": list[JsonValue](dispatched_clicks), "tests": tests}


def mixed_task(_: str) -> str:
    return (
        f"Use open_url to read {DOC_URL} and confirm the record format. "
        f"Navigate to the test page at {URL}\n"
        "Use target=task for each tool's bound URL. Observe the page, fill Locality with 福州, "
        "refresh the snapshot, click Load record, wait for text 鱼丸, and browser_read the resulting record. "
        "Then edit service/core.py AMOUNT, web/component.ts code, and result.json amount and code "
        "using the observed record. Read both code files before editing, and preserve the test files. "
        f"After all edits, use the offered verification choices for {PYTHON_CHECK} in service and {NODE_CHECK} in the root. "
        "Report the observed amount and code and verification results using done."
    )


def natural_mixed_task(_: str) -> str:
    return (f"Update the application's stored fishball record for 福州 to match the live record at {URL}. "
            "The values exposed by the service and web adapter should match that record too. "
            "Follow the project's documented record format. The existing service and web tests must both pass. "
            "Verify the updated application in its browser page. Cite the record and format you actually inspected.")


def mixed_run(directory: Path, target: Target, *, natural: bool = False) -> dict[str, object]:
    if natural:
        oracle = _NATURAL_ORACLE
        fixture = HTML.replace("173", str(oracle.amount)).replace("QUARTZ_851", oracle.code)
        report = run_case(directory, "hybrid", target, intent_override="auto", task_transform=natural_mixed_task,
            project_files=NATURAL_FILES, fixture_html=fixture, max_turns=25,
            declare_required_tools=False, additional_required_tools=(),
            source_pages={NATURAL_FORMAT_URL: NATURAL_FORMAT, NATURAL_VIEW_URL: _verified_application_page},
            independent_score=score_natural_mixed)
        (directory / "mixed-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    report = run_case(directory, "hybrid", target, restore_snapshots=True, intent_override="auto",
        task_transform=mixed_task, project_files=FILES, source_pages={DOC_URL: DOC}, max_turns=25,
        additional_required_tools=("open_url", "run"))
    files_path = directory / "project-files.json"
    files = parse_json_object(files_path.read_text(encoding="utf-8")) if files_path.is_file() else {}
    receipts = tool_receipts(directory / "state")
    successful = [(row.result.call.args.get("command"), row.result.call.args.get("path")) for row in receipts
                  if row.result.call.name == "run" and row.result.ok and row.exit_code == 0]
    checks = ((PYTHON_CHECK, "service"), (NODE_CHECK, "."))
    passed = (report.get("passed") is True and "173" in str(files.get("service/core.py", ""))
              and "QUARTZ_851" in str(files.get("web/component.ts", ""))
              and all(pair in successful for pair in checks)
              and any(row.result.ok and row.result.call.name == "open_url" and row.result.call.args.get("url") == DOC_URL
                      for row in receipts))
    report.update(passed=passed, successful_checks=successful)
    (directory / "mixed-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


class Options(argparse.Namespace):
    run_dir: Path
    natural: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--natural", action="store_true")
    args = parser.parse_args(namespace=Options())
    directory = args.run_dir
    target = replace(select_target(180, "http://127.0.0.1:5001/v1"), context_window_tokens=65536)
    report = mixed_run(directory, target, natural=args.natural)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
