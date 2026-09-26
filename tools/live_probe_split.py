"""Live probe for the PLR/C901 split work (NOT the release gate).

Probes koboldcpp directly (no human watch needed) with scenarios the gate
does NOT cover:

- P0 direct model reply (proves this harness reads kobold replies itself)
- P1 ghost work-queue lifecycle on an isolated state home, incl. invalid
  transitions that must fail clean (exercises the B1 split live)
- P2 agent fix on a hostile fixture: Makefile with tab-indented recipes,
  a symlink shadowing the buggy file, unreadable/binary/oversized files
  (exercises verification discovery, search scan, symlink guard, audit and
  map paths live)
- P3 tiny create task (protocol-repair / stagnation stress + clean done)
- P4 forced project-wide search over the hostile fixture (search scan +
  symlink guard under a reporting task)
- P5 `make lint` + `make test` through the Makefile (headless denies shell;
  proves adaptation, honest reporting, and clean settle)

Verdicts are semantic, not string-contains: crash signals only count on
codey-originated rows, search usage means a tool row with tool == "search",
and recipe-as-target means a `make <t>` outside the Makefile target set
(see the analyzer helpers; unit-tested in tests/test_live_probe_split.py).

Usage:
    python tools/live_probe_split.py --json
    python tools/live_probe_split.py --only p0,p1 --json

Artifacts: .e2e-artifacts/live-probe-<case>.jsonl (gitignored).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.local_discovery import probe_local_endpoint_detail

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / ".e2e-artifacts"
BASE_URLS = ("http://127.0.0.1:5001/v1", "http://localhost:5001/v1")
PROVIDER_ID = "local"
TIMEOUT = 600.0


def _log(text: str) -> None:
    print(text, flush=True)


def probe_endpoint() -> tuple[str, tuple[str, ...]]:
    for base in BASE_URLS:
        endpoint, reason = probe_local_endpoint_detail(base, timeout=5)
        if reason == "ok" and endpoint is not None:
            return endpoint.base_url, endpoint.models
    raise RuntimeError(f"koboldcpp unreachable on {BASE_URLS!r} (is Serve on?)")


def run_p0() -> dict:
    from codey.providers.local_openai import LocalOpenAIProvider

    base_url, models = probe_endpoint()
    model = models[0] if models else "koboldcpp"
    provider = LocalOpenAIProvider(base_url, model, timeout=TIMEOUT)
    t0 = time.time()
    try:
        reply = provider.send("Reply with exactly: PROBE_OK", timeout=TIMEOUT)
    finally:
        provider.close()
    dt = round(time.time() - t0, 1)
    return {
        "case": "p0-direct-reply", "ok": "PROBE_OK" in str(reply),
        "seconds": dt, "model": model, "reply_preview": str(reply)[:300],
    }


def run_p1() -> dict:
    """Work-queue lifecycle incl. invalid transitions (no model)."""
    from codey.ghost.continuity import GhostContinuityStore
    from codey.ghost.work_queue import GhostWorkQueueStore

    state_home = Path(tempfile.mkdtemp(prefix="codey-probe-p1-state-")).resolve()
    notes: list[str] = []
    try:

        class _FakeIndex:
            def recent(self, *a, **k):
                return [{
                    "id": "note-1", "type": "synthesis",
                    "title": "Probe synthesis.",
                    "body": "Probe body.",
                    "open_questions": '["Should the probe keep tracking?"]',
                    "updated": "2999-01-01T00:00:00Z",
                    "session_id": "probe-s1", "project": "",
                }]

        class _FakeKnowledge:
            index = _FakeIndex()

        continuity = GhostContinuityStore(state_home)
        continuity.sync_from_sources(
            knowledge_store=_FakeKnowledge(), session_id="probe-s1",
            run_id="probe-r", mode="chat",
        )
        store = GhostWorkQueueStore(state_home)
        synced = store.sync_from_sources(continuity_store=continuity, session_id="probe-s1")
        notes.append(f"sync_ok={synced.ok}")
        queued = store.list_items(status="queued", session_id="probe-s1")
        if len(queued) != 1:
            return {"case": "p1-work-queue", "ok": False, "notes": notes,
                    "error": f"expected 1 queued item, got {len(queued)}"}
        item_id = queued[0].id
        back_to_queue = store.queue_item(item_id)
        notes.append(f"queue_item_ok={back_to_queue is not None}")
        claim = store.claim_next(session_id="probe-s1", run_id="probe-run",
                                 user_request="continue")
        notes.append(f"claim_ok={claim.ok} reason={claim.skipped_reason}")
        if not claim.ok:
            return {"case": "p1-work-queue", "ok": False, "notes": notes,
                    "error": "claim failed"}
        # Invalid: second claim while running must NOT succeed.
        claim2 = store.claim_next(session_id="probe-s1", run_id="probe-run-2",
                                  user_request="continue")
        notes.append(f"double_claim_ok={claim2.ok} (want False)")
        # Invalid: complete with wrong run must NOT complete.
        bad_complete = store.complete_item(item_id, run_id="wrong-run",
                                           proof_refs=["probe.py:1"])
        notes.append(f"wrong_run_complete={bad_complete is not None} (want False/None)")
        done = store.complete_item(item_id, run_id="probe-run",
                                   proof_refs=["probe.py:1"])
        notes.append(f"complete_status={done.status if done else None}")
        # Invalid: re-complete a done item must NOT return an item.
        redo = store.complete_item(item_id, run_id="probe-run",
                                   proof_refs=["probe.py:1"])
        notes.append(f"recomplete={redo is not None} (want False/None)")
        items = store.list_items(session_id="probe-s1")
        statuses = {i.id: i.status for i in items}
        ok = (
            synced.ok and back_to_queue is not None and claim.ok
            and not claim2.ok and bad_complete is None
            and done is not None and redo is None
            and statuses.get(item_id) in ("done", "blocked")
        )
        return {"case": "p1-work-queue", "ok": ok, "notes": notes,
                "statuses": statuses}
    except Exception as exc:  # noqa: BLE001 - probe must report, not raise
        return {"case": "p1-work-queue", "ok": False, "notes": notes,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(state_home, ignore_errors=True)


def _make_hostile_fixture(root: Path) -> None:
    (root / "pricing.py").write_text(
        "def discounted_price(price, percent):\n"
        "    # PROBE_BUG off by sign\n"
        "    return price * (1 + percent / 100)\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_pricing.py").write_text(
        "import unittest\n\n"
        "from pricing import discounted_price\n\n\n"
        "class PricingTests(unittest.TestCase):\n"
        "    def test_discount(self):\n"
        "        self.assertEqual(discounted_price(100, 20), 80)\n",
        encoding="utf-8",
    )
    # Makefile with tab-indented recipes: recipe lines must never be read
    # as make targets (regression area of the verification split).
    (root / "Makefile").write_text(
        "lint:\n\truff check .\n\ntest:\n\tpython -m pytest\n",
        encoding="utf-8",
    )
    # Symlink shadowing the buggy file: budgeted search must not follow it
    # nor emit duplicate rows (regression area of the references split).
    with contextlib.suppress(OSError):
        (root / "link_pricing.py").symlink_to(root / "pricing.py")
    (root / "binary.dat").write_bytes(bytes(range(256)) * 64)
    (root / "oversized.txt").write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
    try:
        (root / "unreadable.py").write_text("zzz = 1\n", encoding="utf-8")
        (root / "unreadable.py").chmod(0)
    except OSError:
        pass


def _run_agent_probe(case: str, task: str, make_fixture, max_turns: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"codey-probe-{case}-")).resolve()
    state_home = Path(tempfile.mkdtemp(prefix=f"codey-probe-state-{case}-")).resolve()
    rows: list[dict] = []
    try:
        make_fixture(root)
        request = HeadlessRequest(
            project=root, task=task, provider_id=PROVIDER_ID,
            max_turns=max_turns, intent="agent", state_home=state_home,
        )
        t0 = time.time()
        result = run_headless(request, emit_jsonl=rows.append)
        dt = round(time.time() - t0, 1)
        done = next((r for r in reversed(rows) if str(r.get("type") or "") == "task_done"), None)
        stop_reason = str((done or {}).get("stop_reason") or result.stop_reason)
        signals = codey_crash_signals(rows)
        tripwires = {
            **signals,
            "shell_rejected": any(str(r.get("type") or "") == "shell_rejected" for r in rows),
        }
        return {
            "case": case, "seconds": dt, "stop_reason": stop_reason,
            "turns": int((done or {}).get("turns") or 0),
            "exit_code": result.exit_code,
            "summary": str((done or {}).get("summary") or "")[:1000],
            "tripwires": tripwires, "jsonl_rows": len(rows),
            "project": str(root),
            "run_id": result.run_id, "session_id": result.session_id,
            "_rows": rows, "_root": root, "_state_home": state_home,
        }
    except Exception as exc:  # noqa: BLE001
        return {"case": case, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "_rows": rows, "_root": root, "project": str(root)}


def _archive(case: str, rows: list[dict]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARTIFACT_DIR / f"live-probe-{case}.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --- Semantic row analyzers (pure functions, unit-tested) ---
#
# String-contains over the whole JSONL blob cannot prove anything: model and
# test output honestly contains words like "AssertionError" when a fixture
# test fails. These analyzers read STRUCTURED rows instead:
# - crash signals only count on codey-originated rows (never tool results
#   or the model-written task_done summary);
# - "was search used" means a tool row with tool == "search";
# - "recipe taken as target" means a `make <t>` command with t outside the
#   Makefile target set (running the recipe body directly is legitimate).

FAIL_WORDS = ("assertionerror", "failed", "failure", "exit 1")


def _row_text(row: dict) -> str:
    parts = [row.get("summary"), row.get("result"), row.get("message"), row.get("text")]
    return " ".join(str(part) for part in parts if part)


def tool_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("type") or "") == "tool"]


def tool_commands(rows: list[dict]) -> list[str]:
    return [str(row.get("command") or "") for row in tool_rows(rows) if row.get("command")]


def codey_crash_signals(rows: list[dict]) -> dict[str, bool]:
    """True crash evidence, scoped to codey-originated rows only."""
    codey_text = " ".join(
        _row_text(row) for row in rows if str(row.get("type") or "") != "tool"
    )
    # The model-written task_done summary may honestly quote a fixture
    # failure ("AssertionError: 120.0 != 80"); that is evidence of honest
    # reporting, not a codey crash, so it is excluded here and checked by
    # honest_failure_report() instead.
    non_summary = " ".join(
        _row_text(row)
        for row in rows
        if str(row.get("type") or "") not in ("tool", "task_done")
    )
    blob = json.dumps(rows, ensure_ascii=False)
    return {
        # A real interpreter traceback escaping from codey itself. Tool
        # output may honestly paste tracebacks from fixture tests, so tool
        # rows are excluded here.
        "python_traceback": "Traceback (most recent call last)" in codey_text,
        # Runtime state-machine corruption signature; models never write it.
        "illegal_transition": "illegal transition" in blob,
        # Known-fixed map bug signature (None task); same scoping as above.
        "none_task_crash": "NoneType" in codey_text and "strip" in codey_text,
        # An AssertionError outside tool results and outside the model
        # summary means codey itself asserted (e.g. a contract assert).
        "codey_assertion": "AssertionError" in non_summary,
    }


def used_structured_search(rows: list[dict]) -> bool:
    return any(str(row.get("tool") or "") == "search" for row in tool_rows(rows))


def makefile_targets(makefile_text: str) -> tuple[set[str], list[str]]:
    """Split a Makefile into (targets, recipe bodies)."""
    targets: set[str] = set()
    recipes: list[str] = []
    for line in makefile_text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            name = line.split(":")[0].strip()
            if name and not name.startswith(("#", ".")):
                targets.add(name)
        elif line.startswith("\t") and line.strip():
            recipes.append(line.strip())
    return targets, recipes


def make_misuse(commands: list[str], targets: set[str]) -> list[str]:
    """Commands that invoke a non-target as `make <t>`.

    Running a recipe body directly (e.g. `ruff check .`) is legitimate
    adaptation, not misuse; only `make` with an unknown target counts.
    """
    bad: list[str] = []
    for command in commands:
        text = command.strip()
        if not text.startswith("make "):
            continue
        target = text[5:].strip().strip("\"'")
        if target and target not in targets:
            bad.append(command)
    return bad


def honest_failure_report(rows: list[dict], summary: str) -> bool:
    """A failure-quoting summary must be backed by a failed tool row."""
    if not any(word in summary.lower() for word in FAIL_WORDS):
        return True
    return any(not row.get("ok", True) for row in tool_rows(rows))


def evaluate_p5_semantics(rows: list[dict], summary: str) -> dict[str, object]:
    """Prove `make lint / make test` reached their intended semantics.

    Accepts honest adaptation: when `make` is missing, running the recipe
    bodies (`ruff check .`, `python -m pytest`) directly still counts, as
    long as lint + test both demonstrably ran and the reported outcome is
    backed by tool rows.
    """
    commands = tool_commands(rows)
    make_rows = [row for row in tool_rows(rows) if str(row.get("command") or "").strip().startswith("make ")]
    lint_ran = any(
        cmd.strip() == "make lint" or cmd.strip() == "ruff check ."
        for cmd in commands
    )
    test_ran = any(
        cmd.strip() == "make test" or cmd.strip().startswith("python -m pytest")
        for cmd in commands
    )
    signals = codey_crash_signals(rows)
    findings = {
        "make_attempted": bool(make_rows),
        # A structured outcome (clean ERROR or a real exit) as opposed to
        # a crash; crash-freedom itself is covered by crash_signals.
        "make_clean": bool(make_rows) and all(str(row.get("result") or "") for row in make_rows),
        "lint_ran": lint_ran,
        "test_ran": test_ran,
        "honest_report": honest_failure_report(rows, summary),
        "crash_signals": signals,
    }
    findings["ok"] = bool(
        findings["make_attempted"]
        and findings["lint_ran"]
        and findings["test_ran"]
        and findings["honest_report"]
        and not any(signals.values())
    )
    return findings


P2_TASK = (
    "Fix the sign bug in pricing.py discounted_price (20% off 100 must be 80), "
    "then verify with the project's own checks and report which checks you ran."
)

P4_TASK = (
    "Do NOT edit anything. Search the whole project for every occurrence of "
    "the word 'discount' and report each hit as file:line. Include test files."
)

P5_TASK = (
    "Verify this project using its Makefile targets: run `make lint` and "
    "`make test`, then report the results."
)


def run_p2() -> dict:
    data = _run_agent_probe("p2-hostile-fix", P2_TASK, _make_hostile_fixture, 8)
    rows, root = data.pop("_rows", []), Path(data.pop("_root"))
    state_home = data.pop("_state_home", None)
    findings: dict[str, object] = {}
    try:
        if data.get("error"):
            data["findings"] = findings
            return data
        # Independent verification: bug fixed + suite green.
        fixed_src = (root / "pricing.py").read_text(encoding="utf-8")
        findings["bug_text_gone"] = "1 + percent" not in fixed_src
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_pricing.py"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        findings["suite_exit"] = proc.returncode
        findings["suite_tail"] = (proc.stdout + proc.stderr)[-300:]
        # Recipe lines must never surface as `make` targets: parse the
        # fixture Makefile and check every invoked `make <t>`.
        targets, _ = makefile_targets(
            "lint:\n\truff check .\n\ntest:\n\tpython -m pytest\n"
        )
        misuse = make_misuse(tool_commands(rows), targets)
        findings["make_misuse"] = misuse
        # Symlink must not appear in any structured tool result.
        tool_text = " ".join(
            str(row.get("result") or "") for row in tool_rows(rows)
        )
        findings["link_in_tool_results"] = "link_pricing.py" in tool_text
        data["ok"] = (
            bool(findings["bug_text_gone"]) and proc.returncode == 0
            and not misuse
            and "link_pricing.py" not in tool_text
            and not any(codey_crash_signals(rows).values())
        )
        data["findings"] = findings
        return data
    finally:
        _archive("p2-hostile-fix", rows)
        with contextlib.suppress(OSError):
            (root / "unreadable.py").chmod(0o666)
        shutil.rmtree(root, ignore_errors=True)
        if state_home:
            shutil.rmtree(state_home, ignore_errors=True)


def run_p3() -> dict:
    def _fixture(root: Path) -> None:
        return None

    data = _run_agent_probe(
        "p3-tiny-create",
        "Create notes.txt containing exactly: hello probe",
        _fixture, 6,
    )
    rows, root = data.pop("_rows", []), Path(data.pop("_root"))
    state_home = data.pop("_state_home", None)
    try:
        if data.get("error"):
            return data
        note = root / "notes.txt"
        content = note.read_text(encoding="utf-8").strip() if note.exists() else ""
        data["note_ok"] = content == "hello probe"
        data["note_content"] = content[:100]
        data["ok"] = (
            bool(data["note_ok"]) and data.get("stop_reason") == "done"
            and not any(codey_crash_signals(rows).values())
        )
        return data
    finally:
        _archive("p3-tiny-create", rows)
        shutil.rmtree(root, ignore_errors=True)
        if state_home:
            shutil.rmtree(state_home, ignore_errors=True)


def run_p4() -> dict:
    """Force project-wide search over the hostile fixture."""
    data = _run_agent_probe("p4-search-sweep", P4_TASK, _make_hostile_fixture, 8)
    rows, root = data.pop("_rows", []), Path(data.pop("_root"))
    state_home = data.pop("_state_home", None)
    try:
        if data.get("error"):
            return data
        tool_text = " ".join(
            str(row.get("result") or "") for row in tool_rows(rows)
        )
        data["link_in_tool_results"] = "link_pricing.py" in tool_text
        data["used_search"] = used_structured_search(rows)
        data["ok"] = (
            data.get("stop_reason") == "done"
            and "link_pricing.py" not in tool_text
            and not any(codey_crash_signals(rows).values())
        )
        return data
    finally:
        _archive("p4-search-sweep", rows)
        with contextlib.suppress(OSError):
            (root / "unreadable.py").chmod(0o666)
        shutil.rmtree(root, ignore_errors=True)
        if state_home:
            shutil.rmtree(state_home, ignore_errors=True)


def run_p5() -> dict:
    """Force `make` usage: headless denies shell, settle must stay clean."""
    data = _run_agent_probe("p5-make-shell-deny", P5_TASK, _make_hostile_fixture, 8)
    rows, root = data.pop("_rows", []), Path(data.pop("_root"))
    state_home = data.pop("_state_home", None)
    try:
        if data.get("error"):
            return data
        summary = str(data.get("summary") or "")
        findings = evaluate_p5_semantics(rows, summary)
        data["findings"] = findings
        data["ok"] = bool(findings["ok"])
        return data
    finally:
        _archive("p5-make-shell-deny", rows)
        with contextlib.suppress(OSError):
            (root / "unreadable.py").chmod(0o666)
        shutil.rmtree(root, ignore_errors=True)
        if state_home:
            shutil.rmtree(state_home, ignore_errors=True)


_CASES: tuple[tuple[str, object, int], ...] = (
    ("p0", run_p0, 300),
    ("p1", run_p1, 500),
    ("p2", run_p2, 800),
    ("p3", run_p3, 800),
    ("p4", run_p4, 800),
    ("p5", run_p5, 800),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=",".join(name for name, _, _ in _CASES))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    selected = [s.strip() for s in str(args.only).split(",") if s.strip()]
    results: list[dict] = []
    for name, func, preview in _CASES:
        if name in selected:
            results.append(func())  # type: ignore[operator]
            _log(json.dumps(results[-1], ensure_ascii=False)[:preview])
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            _log(f"{r.get('case')}: ok={r.get('ok')}")
    failed = [r for r in results if not r.get("ok")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
