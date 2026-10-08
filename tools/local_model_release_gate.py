"""Local-model release gate for Codey (auto-captured, no human watch needed).

Runs chat + agent (create/edit/references/hybrid/discussion/planning/auto) + ghost
against a local OpenAI-compatible endpoint (KoboldCpp is the default
http://127.0.0.1:5001/v1), captures headless JSONL per case into
.e2e-artifacts/local-model-release-<unique-run>/<attempt>-<case>/, and verifies independently of the
model's own claims (like tools/live_smoke.py does for web providers).

Verified scope (and only this scope):
- with-hybrid-intent start, final files correct, independent ``unittest``
  verification passes, and the task reports ``done``;
- hybrid additionally proves ordered tool use on one session:
  ``web_search -> open -> read_file -> edit -> run -> done`` with a single
  ``run_id``/``session_id`` across JSONL rows.
It does not prove web-search relevance, citation quality, or multi-session
behavior beyond the recorded rows.

Usage:
    python tools/local_model_release_gate.py --json
    python tools/local_model_release_gate.py --case hybrid --json

Exit 0 only when every selected case passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.request import urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV
from codey.providers.local_discovery import LOCAL_BASE_URL_CANDIDATES, probe_local_endpoint_detail
from tools import local_model_gate_attempts as attempts
from tools.local_model_gate_recovery import run_recovery_case

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / ".e2e-artifacts"
DEFAULT_BASE_URLS = LOCAL_BASE_URL_CANDIDATES
PROVIDER_ID = "local"
TIMEOUT = 600.0

CASES = (
    "chat", "read", "create", "edit", "references", "hybrid", "discussion", "planning", "auto", "ghost",
    "tests", "research", "recovery", "review", "project_review",
)
DEFAULT_CASES = CASES


def _log(text: str) -> None:
    print(text, flush=True)


def candidate_base_urls() -> tuple[str, ...]:
    configured = os.environ.get(LOCAL_OPENAI_BASE_URL_ENV, "").strip().rstrip("/")
    if configured:
        return (configured,)
    return tuple(DEFAULT_BASE_URLS)


def probe_endpoint() -> tuple[str, tuple[str, ...]]:
    bases = candidate_base_urls()
    for base in bases:
        endpoint, reason = probe_local_endpoint_detail(base, timeout=5)
        if reason == "ok" and endpoint is not None:
            return endpoint.base_url, endpoint.models
    raise RuntimeError(f"local model endpoint unreachable: probe /models failed on {bases!r} (is the server on?)")


def _make_fixture(root: Path, case: str) -> None:
    if case in {"create", "discussion", "auto", "research"}:
        return
    if case == "tests":
        (root / "calculator.py").write_text(
            "def multiply(left, right):\n    return left * right\n", encoding="utf-8",
        )
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        return
    if case == "planning":
        _make_fixture(root, "references")
        return
    if case == "read":
        (root / "pricing.py").write_text(
            "def discounted_price(price, percent):\n    return price * (1 - percent / 100)\n", encoding="utf-8",
        )
        return
    if case in {"edit", "hybrid"}:
        # Discoverable shape: tests/ dir so verification discovery finds
        # `python -m unittest discover`. A bare test_*.py at root yields NO
        # candidate and blocks by design (fail-closed); the gate must not
        # use that shape for the happy path.
        (root / "pricing.py").write_text(
            "def discounted_price(price, percent):\n"
            "    # LIVE_SMOKE_BUG\n"
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
        return
    if case == "references":
        (root / "pricing.py").write_text(
            "\n".join(f"# filler {index}" for index in range(120))
            + "\n\n"
            "def calculate_total(amount, tax_rate, discount=0):\n"
            "    # LIVE_SMOKE_REFERENCE_TARGET\n"
            "    return amount * (1 + tax_rate)\n",
            encoding="utf-8",
        )
        (root / "checkout.py").write_text(
            "from pricing import calculate_total\n\n\n"
            "def checkout_total():\n"
            "    return calculate_total(100, 0.2)\n\n\n"
            "def discounted_checkout_total():\n"
            "    return calculate_total(100, 0.2)\n",
            encoding="utf-8",
        )
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests" / "test_checkout.py").write_text(
            "import unittest\n\n"
            "from checkout import checkout_total, discounted_checkout_total\n"
            "from pricing import calculate_total\n\n\n"
            "class CheckoutTests(unittest.TestCase):\n"
            "    def test_existing_total_still_works(self):\n"
            "        self.assertEqual(checkout_total(), 120)\n\n"
            "    def test_discount_argument_affects_total(self):\n"
            "        self.assertEqual(calculate_total(100, 0.2, discount=10), 108)\n\n"
            "    def test_discounted_checkout_uses_discount(self):\n"
            "        self.assertEqual(discounted_checkout_total(), 108)\n",
            encoding="utf-8",
        )
        return
    raise ValueError(f"unknown fixture: {case}")


def _task_for(case: str) -> tuple[str, str, int]:
    if case == "read":
        return (
            "Use read_file to inspect pricing.py. Explain its formula without changing any files. "
            "Finish with done whose summary contains your explanation.", "planning_readonly", 4,
        )
    if case == "create":
        return (
            "Create math_utils.py with add(a, b) returning a + b. "
            "Create tests/__init__.py (empty) and tests/test_math_utils.py with a unittest for add(2, 3) == 5. "
            "Use edit with content to create the files; edit creates parent directories automatically. "
            "Do not run mkdir or request shell commands. Run python -m unittest discover "
            "and finish with done. Do not remove or weaken tests.",
            "project",
            10,
        )
    if case == "edit":
        return (
            "Fix the LIVE_SMOKE_BUG in pricing.py. Use grep and read_file before edit, "
            "then run python -m unittest discover and finish with done. Do not modify the existing tests.",
            "project",
            10,
        )
    if case == "references":
        return (
            "Update calculate_total(amount, tax_rate, discount=0) so it subtracts "
            "discount before tax, i.e. (amount - discount) * (1 + tax_rate). "
            "Keep checkout_total() unchanged; discounted_checkout_total() must pass discount=10. Before editing, use "
            "find_references for calculate_total, then read_file for the real source "
            "near each affected location. Run python -m unittest discover and finish with done. "
            "Do not modify the existing tests.",
            "project",
            12,
        )
    if case == "hybrid":
        return (
            "Use the shared hybrid task path. First web_search for the pricing "
            "discount context, then open_url the relevant source, "
            "then read_file pricing.py, fix LIVE_SMOKE_BUG with edit, "
            "run python -m unittest discover, and finish with done. "
            "Do the steps in order: web_search -> open_url -> read_file -> edit -> run -> done. "
            "Do not modify the existing tests.",
            "hybrid",
            12,
        )
    if case == "discussion":
        return (
            "Discuss a good first version of a breathing practice app. "
            "Give the user a concrete, concise recommendation without creating "
            "or editing files. Finish with done whose summary is the direct answer.",
            "project",
            6,
        )
    if case == "planning":
        return (
            "Plan how to add a discount argument to calculate_total without editing files. "
            "List the files and steps only, then finish with done.",
            "planning_readonly",
            6,
        )
    if case == "auto":
        return (
            "Create hello_auto.txt with exactly the text hello auto and nothing else, "
            "then finish with done.",
            "auto",
            8,
        )
    if case == "tests":
        return (
            "Add tests/test_calculator.py with unittest tests for calculator.multiply. "
            "Cover positive, zero, negative, and decimal inputs. Use edit with content; "
            "do not change calculator.py or create shell commands. Run python -m unittest discover "
            "and finish with done.", "project", 8,
        )
    if case == "research":
        return (
            "Research the official Python pathlib documentation. First use web_search, then open_url "
            "on a result, then finish with done summarizing the source. Do not create or edit files.",
            "research", 8,
        )
    raise ValueError(f"unknown agent case: {case}")


def _ran_zero_tests(output: str) -> bool:
    """unittest exits 0 with 'Ran 0 tests / OK' when nothing was discovered."""
    return re.search(r"Ran\s+0\s+tests?\b", output) is not None


def _canonical_tool_name(row: dict) -> str:
    """Canonical tool name for gate assertions; display ``tool`` is ignored.

    Headless audit rows carry ``tool_name`` (canonical ``event.call.name``)
    alongside ``tool`` (UI display). The gate must only read ``tool_name``:
    display names (``search``/``read``) cannot prove which tool ran.
    """
    return str(row.get("tool_name") or "").strip().lower()


def _tool_names_in_order(rows: list[dict]) -> list[str]:
    """Canonical tool names in JSONL order; task_done folds to ``done``."""
    names: list[str] = []
    for row in rows or []:
        rtype = str(row.get("type") or "")
        if rtype == "tool":
            name = _canonical_tool_name(row)
            if name:
                names.append(name)
        elif rtype == "task_done":
            names.append("done")
    return names


def _normalize_hybrid_step(name: str) -> str:
    name = str(name or "").strip().lower()
    if name in {"open_url", "open_result", "reopen_source", "open_hit"}:
        return "open"
    return name


_TASK_IDENTITY_TYPES = frozenset({"task_start", "turn", "tool_started", "tool", "task_done"})


def _row_ok(row: dict) -> bool:
    return bool(row.get("ok") is True)


def _run_row_ok(row: dict) -> bool:
    if not _row_ok(row):
        return False
    from codey.utils.refs import strict_exit_code

    code = strict_exit_code(row.get("exit_code"))
    return code is not None and code == 0


def check_hybrid_tool_order(rows: list[dict]) -> dict:
    """Deterministic order assertion: search -> open -> read -> edit -> run -> done.

    Returns ``{"ok": bool, "tool_names": [...], "detail": str}``. Hybrid proves
    the shared path in order; files+done alone do not pass. Only canonical
    ``tool_name`` counts; every tool/task_done row must carry a consistent
    run_id/session_id (proves the same Codey run/session, not just event
   归属). Same ids cannot prove the provider never called new_chat().
    Every required tool step must carry ok=True; ``run`` additionally
    requires a structured zero exit_code (text never implies pass).
    """
    relevant = [r for r in (rows or []) if str(r.get("type") or "") in {"tool", "task_done"}]
    for row in relevant:
        if not str(row.get("run_id") or "") or not str(row.get("session_id") or ""):
            return {
                "ok": False,
                "tool_names": _tool_names_in_order(rows),
                "detail": f"missing run_id/session_id in {row}",
            }
    run_ids = {str(r.get("run_id") or "") for r in relevant}
    sess_ids = {str(r.get("session_id") or "") for r in relevant}
    if len(run_ids) != 1 or len(sess_ids) != 1:
        return {
            "ok": False,
            "tool_names": _tool_names_in_order(rows),
            "detail": f"split session in hybrid order: run_ids={sorted(run_ids)} session_ids={sorted(sess_ids)}",
        }
    names = _tool_names_in_order(rows)
    # A row without tool_name contributes nothing; display-only rows can never
    # satisfy the canonical order.
    if any(str(r.get("type") or "") == "tool" and not _canonical_tool_name(r) for r in relevant):
        return {
            "ok": False,
            "tool_names": names,
            "detail": f"missing canonical tool_name in {names}",
        }
    # Ordered successful evidence: each required step must be ok, run needs
    # structured zero exit. Walk rows in order so extra retries do not help.
    want = ["web_search", "open", "read_file", "edit", "run", "done"]
    idx = 0
    ordered = [r for r in (rows or []) if str(r.get("type") or "") in {"tool", "task_done"}]
    for step in want:
        found = -1
        for pos in range(idx, len(ordered)):
            row = ordered[pos]
            rtype = str(row.get("type") or "")
            if rtype == "task_done":
                name = "done"
                ok = str(row.get("stop_reason") or "") == "done"
            else:
                name = _normalize_hybrid_step(_canonical_tool_name(row))
                ok = _run_row_ok(row) if name == "run" else _row_ok(row)
            if name == step and ok:
                found = pos
                break
        if found < 0:
            return {
                "ok": False,
                "tool_names": names,
                "detail": f"missing successful step {step!r} in {names}",
            }
        idx = found + 1
    return {"ok": True, "tool_names": names, "detail": "search->open->read->edit->run->done"}


def check_research_tool_order(rows: list[dict]) -> dict:
    """Require successful web search, source opening, and terminal done evidence."""
    relevant = [r for r in (rows or []) if str(r.get("type") or "") in {"tool", "task_done"}]
    identities = {(str(r.get("run_id") or ""), str(r.get("session_id") or "")) for r in relevant}
    if not relevant or any(not run or not session for run, session in identities) or len(identities) != 1:
        return {"ok": False, "tool_names": _tool_names_in_order(rows), "detail": "invalid task identity"}
    successful: list[str] = []
    for row in relevant:
        if str(row.get("type") or "") == "task_done":
            if str(row.get("stop_reason") or "") == "done":
                successful.append("done")
        elif row.get("ok") is True:
            name = _normalize_hybrid_step(_canonical_tool_name(row))
            if name:
                successful.append(name)
    try:
        search = successful.index("web_search")
        opened = successful.index("open", search + 1)
        done = successful.index("done", opened + 1)
    except ValueError:
        return {"ok": False, "tool_names": _tool_names_in_order(rows), "detail": f"missing search/open/done in {successful}"}
    return {"ok": True, "tool_names": _tool_names_in_order(rows), "detail": "search->open->done", "positions": [search, opened, done]}


def check_single_session_identity(rows: list[dict]) -> dict:
    """Task-run rows must share one run_id/session_id (same Codey run/session).

    Only task-run events participate (task_start, turn, tool_started, tool,
    task_done); global connection status and other run-level rows carry no
    task identity and are ignored. Each participating row must carry
    non-empty consistent ids, and at least task_start and task_done must be
    present. Same ids prove the same Codey run/session attribution only;
    they cannot prove the provider never called new_chat().
    """
    rows = list(rows or [])
    task_rows = [r for r in rows if str(r.get("type") or "") in _TASK_IDENTITY_TYPES]
    if not task_rows:
        return {"ok": False, "run_ids": [], "session_ids": [], "detail": "no task rows"}
    missing = [
        r for r in task_rows if not str(r.get("run_id") or "") or not str(r.get("session_id") or "")
    ]
    if missing:
        run_ids = sorted({str(r.get("run_id") or "") for r in task_rows})
        sess_ids = sorted({str(r.get("session_id") or "") for r in task_rows})
        return {
            "ok": False,
            "run_ids": run_ids,
            "session_ids": sess_ids,
            "detail": f"missing run_id/session_id in {len(missing)} task row(s); run_ids={run_ids} session_ids={sess_ids}",
        }
    run_ids = {str(r.get("run_id") or "") for r in task_rows}
    sess_ids = {str(r.get("session_id") or "") for r in task_rows}
    kinds = {str(r.get("type") or "") for r in task_rows}
    if "task_start" not in kinds or "task_done" not in kinds:
        return {
            "ok": False,
            "run_ids": sorted(run_ids),
            "session_ids": sorted(sess_ids),
            "detail": f"missing task_start/task_done in {sorted(kinds)}",
        }
    ok = len(run_ids) == 1 and len(sess_ids) == 1
    detail = f"run_ids={sorted(run_ids)} session_ids={sorted(sess_ids)}"
    return {"ok": ok, "run_ids": sorted(run_ids), "session_ids": sorted(sess_ids), "detail": detail}


def fixture_test_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "tests").rglob("*.py"))
    }


def fixture_file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file() and "__pycache__" not in path.parts
    }


def _verify_fixture(
    root: Path, case: str, *, baseline_tests: dict[str, str] | None = None,
    baseline_files: dict[str, str] | None = None,
) -> dict:
    if baseline_tests is not None:
        current = fixture_test_hashes(root)
        changed = [name for name, digest in baseline_tests.items() if current.get(name) != digest]
        if changed:
            return {"ok": False, "exit_code": None, "output": f"existing tests changed: {changed}"}
    if case in {"discussion", "planning", "read", "research"}:
        unchanged = baseline_files is not None and fixture_file_hashes(root) == baseline_files
        return {
            "ok": unchanged, "exit_code": 0 if unchanged else 1,
            "output": "no files changed" if unchanged else "read-only files changed or baseline missing",
        }
    if case == "tests":
        test_path = root / "tests" / "test_calculator.py"
        if not test_path.exists():
            return {"ok": False, "exit_code": 1, "output": "tests/test_calculator.py missing"}
        commands = ([sys.executable, "-B", "-m", "unittest", "discover"],)
        assertion = ""
    elif case == "create":
        assertion = (
            "from math_utils import add; "
            "assert all(add(a, b) == a + b for a, b in "
            "[(2, 3), (0, 0), (-3, 8), (17, 24), (-5, -7), (1.25, 2.5)])"
        )
    elif case in {"edit", "hybrid"}:
        assertion = (
            "from math import isclose; from pricing import discounted_price; "
            "assert all(isclose(discounted_price(price, percent), price * (1 - percent / 100)) "
            "for price, percent in [(100, 20), (50, 10), (0, 25), (80, 0), (42, 100), (12.5, 12.5)])"
        )
    elif case == "references":
        assertion = (
            "from checkout import checkout_total, discounted_checkout_total; "
            "from pricing import calculate_total; "
            "assert checkout_total() == 120; "
            "assert calculate_total(100, 0.2, discount=10) == 108; "
            "assert discounted_checkout_total() == 108; "
            "from math import isclose; "
            "assert all(isclose(calculate_total(a, t, discount=d), (a - d) * (1 + t)) "
            "for a, t, d in [(50, 0, 5), (240, 0.1, 20), (0, 0.2, 0), (12.5, 0.05, 1.25)])"
        )
    elif case == "auto":
        target = root / "hello_auto.txt"
        if not target.exists():
            return {"ok": False, "exit_code": 1, "output": "hello_auto.txt missing"}
        content = target.read_text(encoding="utf-8", errors="replace")
        # Exact content: substring matching would pass trailing junk, which
        # defeats the independent deterministic verification this gate claims.
        ok = content.rstrip("\r\n") == "hello auto"
        return {"ok": ok, "exit_code": 0 if ok else 1, "output": content[:500]}
    else:
        raise ValueError(f"unknown fixture: {case}")
    if case != "tests":
        commands = (
            [sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); " + assertion, str(root)],
            [sys.executable, "-B", "-m", "unittest", "discover"],
        )
    outputs: list[str] = []
    for command in commands:
        try:
            proc = subprocess.run(
                command, cwd=root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": None, "output": "independent verification timed out after 120s"}
        output = "\n".join(part for part in (proc.stdout.rstrip(), proc.stderr.rstrip()) if part)
        if output:
            outputs.append(output)
        if proc.returncode != 0:
            return {"ok": False, "exit_code": proc.returncode, "output": "\n\n".join(outputs)[-4000:]}
    combined = "\n\n".join(outputs)[-4000:]
    if _ran_zero_tests(combined):
        return {"ok": False, "exit_code": 1, "output": combined}
    if case == "create":
        meaningful = _verify_generated_tests(root)
        if meaningful is not None:
            return meaningful
    if case == "tests":
        with tempfile.TemporaryDirectory(prefix="codey-gate-tests-mutant-") as temporary:
            mutant = Path(temporary) / "project"
            shutil.copytree(root, mutant, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
            (mutant / "calculator.py").write_text(
                "def multiply(left, right):\n    return left + right\n", encoding="utf-8",
            )
            mutant_run = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover"], cwd=mutant,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
            )
            if mutant_run.returncode == 0 or _ran_zero_tests(mutant_run.stdout + mutant_run.stderr):
                return {"ok": False, "exit_code": mutant_run.returncode, "output": "generated tests accept wrong multiply"}
    return {"ok": True, "exit_code": 0, "output": combined}


def _verify_generated_tests(root: Path) -> dict | None:
    """Require the generated suite to distinguish addition from subtraction.

    Mutate an isolated copy, never the project the agent just completed.
    This establishes sensitivity to one wrong implementation, not coverage.
    """
    with tempfile.TemporaryDirectory(prefix="codey-gate-mutant-") as temporary:
        mutant = Path(temporary) / "project"
        shutil.copytree(root, mutant, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        (mutant / "math_utils.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover"], cwd=mutant,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": None, "output": "generated test sensitivity check timed out"}
        if result.returncode == 0 or _ran_zero_tests(result.stdout + result.stderr):
            return {"ok": False, "exit_code": None, "output": "generated tests accept wrong addition"}
    return None


def run_chat_case(target: attempts.GateTarget, case_dir: Path) -> dict:
    provider = attempts.make_provider(target, case_dir)
    t0 = time.perf_counter()
    try:
        reply = provider.send("Reply with exactly: KOBOLD_OK", timeout=target.request_timeout)
    finally:
        provider.close()
    dt = round(time.perf_counter() - t0, 1)
    ok = str(reply).strip() == "KOBOLD_OK"
    return {
        "case": "chat", "ok": ok, "seconds": dt,
        "base_url": target.base_url, "model": target.model,
        "failure_stage": "" if ok else "chat_marker_mismatch",
        "reply_preview": str(reply)[:500],
    }


def run_agent_case(case: str, *, target: attempts.GateTarget, case_dir: Path) -> dict:
    task, intent, max_turns = _task_for(case)
    max_turns = target.turn_budget or max_turns
    input_path = case_dir / "input.json"
    config = json.loads(input_path.read_text(encoding="utf-8"))
    root = Path(config["project"])
    state_home = Path(config["state"])
    rows: list[dict] = []
    def record_event(row):
        rows.append(row)
        with (case_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    try:
        _make_fixture(root, case)
        baseline_tests = fixture_test_hashes(root)
        baseline_files = fixture_file_hashes(root)
        attempts.write_json(case_dir / "baseline-files.json", baseline_files)
        request = HeadlessRequest(
            project=root, task=task, provider_id=target.provider_id,
            model_selection=({"model": target.model} if target.provider_id != "local" else {}),
            max_turns=max_turns, intent=intent, state_home=state_home,
            sources_open_required=(case == "hybrid"),
            project_changes_required=(case in attempts.OBJECTIVE_CASES),
            research_store_root=(state_home / "research-vault" if case == "research" else None),
        )
        t0 = time.perf_counter()
        result = run_headless(
            request, emit_jsonl=record_event,
            connect_provider=lambda provider_id, **kwargs: _connect_gate_provider(provider_id, target, case_dir),
            connect_reviewer=(lambda provider_id: _connect_gate_provider(provider_id, target, case_dir))
            if intent in {"project", "hybrid", "auto", "review"} and target.provider_id == "local" else None,
        )
        dt = round(time.perf_counter() - t0, 1)
        done = next((r for r in reversed(rows) if str(r.get("type") or "") == "task_done"), None)
        verification = _verify_fixture(root, case, baseline_tests=baseline_tests, baseline_files=baseline_files)
        stop_reason = str((done or {}).get("stop_reason") or result.stop_reason)
        tool_names = _tool_names_in_order(rows)
        single_session = check_single_session_identity(rows)
        # Hybrid additionally proves ordered tool use on one session:
        # search -> open -> read -> edit -> run -> done. Other cases prove
        # files+done+independent verification only (see module docstring).
        hybrid_order: dict | None = None
        if case == "hybrid":
            hybrid_order = check_hybrid_tool_order(rows)
        research_order: dict | None = None
        if case == "research":
            research_order = check_research_tool_order(rows)
        order = hybrid_order or research_order
        ok = (
            stop_reason == "done"
            and result.exit_code == 0
            and done is not None
            and verification["ok"]
            and single_session["ok"]
            and (order is None or bool(order.get("ok")))
            and (case != "read" or any(r.get("tool_name") == "read_file" and r.get("ok") is True for r in rows))
        )
        failure_stage = _agent_failure_stage(ok, verification, single_session, order, stop_reason)
        if (
            case == "research" and not any(r.get("tool_name") == "web_search" for r in rows)
            and "not configured" in str((done or {}).get("summary") or "").lower()
        ):
            failure_stage = "research_environment_unavailable"
        data = {
            "case": case, "ok": ok, "seconds": dt,
            "stop_reason": stop_reason, "turns": int((done or {}).get("turns") or 0),
            "exit_code": result.exit_code,
            "summary": str((done or {}).get("summary") or "")[:1000],
            "verification": verification,
            "work_correct": verification["ok"],
            "failure_stage": failure_stage,
            "project": str(root),
            "run_id": result.run_id, "session_id": result.session_id,
            "jsonl_rows": len(rows),
            "tool_names": tool_names,
            "tool_order": order,
            "single_session": single_session,
        }
    except Exception as exc:  # noqa: BLE001 - gate must report, not raise
        data = {"case": case, "ok": False, "failure_stage": "agent_exception",
                "error": f"{type(exc).__name__}: {exc}", "project": str(root)}
    finally:
        (case_dir / "events.jsonl").touch(exist_ok=True)
    return data


def _connect_gate_provider(provider_id: str, target: attempts.GateTarget, directory: Path):
    if provider_id != target.provider_id:
        raise RuntimeError(f"gate pins provider {target.provider_id}; refusing failover to {provider_id}")
    return attempts.make_provider(target, directory)


def _agent_failure_stage(ok, verification, identity, order, stop_reason):
    if ok:
        return ""
    if verification.get("ok") is not True:
        return "independent_verification"
    if identity.get("ok") is not True:
        return "session_identity"
    if order is not None and order.get("ok") is not True:
        return "tool_order"
    return "completion:" + stop_reason


def run_ghost_case() -> dict:
    """Ghost control-plane roundtrip inside an isolated state home.

    Writes one observation for this run, reads it back by run_id, checks
    retrieval surfaces it, deletes the session scope, and asserts it is
    no longer retrievable. The user's default state is not part of this test.
    """
    from codey.ghost.observation_index import retrieve_relevant_observations
    from codey.ghost.observations import GhostObservationStore
    state_home = Path(tempfile.mkdtemp(prefix="codey-kobold-gate-ghost-")).resolve()
    try:
        store = GhostObservationStore(state_home)
        run_id = "gate-ghost-roundtrip"
        session_id = "gate-session"
        if not store.append_completed(
            run_id=run_id, session_id=session_id, project="",
            mode="chat", user_text="gate breathing plan",
            assistant_text="gate reply about breathing",
            stop_reason="done", provider_id="local",
        ):
            return {"case": "ghost", "ok": False, "error": "roundtrip append failed"}
        committed = store.read_committed(session_id=session_id)
        seen = [row for row in committed if str(row.get("run_id") or "") == run_id]
        if not seen:
            return {"case": "ghost", "ok": False, "error": "roundtrip read by run_id failed"}
        picked = retrieve_relevant_observations(
            committed, "breathing plan", exclude_run_id="other-run",
        )
        if not any(str(row.get("run_id") or "") == run_id for row in picked):
            return {"case": "ghost", "ok": False, "error": "roundtrip retrieval missed"}
        removed = store.delete_scope("session", session_id=session_id)
        if removed < 1:
            return {"case": "ghost", "ok": False, "error": "roundtrip delete removed nothing"}
        after = [row for row in store.read_committed(session_id=session_id)
                 if str(row.get("run_id") or "") == run_id]
        if after:
            return {"case": "ghost", "ok": False, "error": "deleted observation still retrievable"}
        return {
            "case": "ghost", "ok": True, "observations": len(committed),
            "roundtrip": "write->read->retrieve->delete->gone",
        }
    except Exception as exc:  # noqa: BLE001
        return {"case": "ghost", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(state_home, ignore_errors=True)


def _worker(case: str, directory: Path) -> int:
    config = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    target = attempts.GateTarget(**config["target"])
    recorder = attempts.record_api_generations(directory / "provider.jsonl") if target.provider_id != "local" else nullcontext()
    with recorder, attempts.isolate_gate_secondary_models(target):
        return _run_worker(case, directory, target)


def _run_worker(case: str, directory: Path, target: attempts.GateTarget) -> int:
    from codey.env_names import NATIVE_TOOLS_ENV

    os.environ[NATIVE_TOOLS_ENV] = "1" if target.protocol == "native" else "0"
    try:
        if case == "chat":
            data = run_chat_case(target, directory)
        elif case == "ghost":
            data = run_ghost_case()
        elif case == "recovery":
            data = run_recovery_case(target, directory)
        elif case == "review":
            from tools.local_model_gate_review import run_review_case

            data = run_review_case(target, directory)
        elif case == "project_review":
            from tools.local_model_gate_project_review import run_project_review_case

            data = run_project_review_case(target, directory)
        else:
            data = run_agent_case(case, target=target, case_dir=directory)
    except Exception as exc:
        data = {"case": case, "ok": False, "failure_stage": "worker_exception",
                "error": f"{type(exc).__name__}: {exc}"}
    attempts.write_json(directory / "worker-result.json", data)
    return 0  # Transport succeeded; data.ok owns the case verdict.


def _server_observations(base_url: str) -> dict:
    from urllib.parse import urlsplit

    parsed = urlsplit(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    observations = {}
    for path in ("/api/extra/version", "/api/v1/config/max_context_length", "/api/v1/config/max_length"):
        try:
            with urlopen(origin + path, timeout=5) as response:
                observations[path] = json.loads(response.read(8192))
        except Exception as exc:
            observations[path] = {"unavailable": type(exc).__name__}
    return observations


def _metadata(target: attempts.GateTarget) -> dict:
    from dataclasses import asdict

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=attempts.REPO_ROOT,
                            capture_output=True, text=True, check=False, timeout=10)
    metadata = {
        "target": asdict(target), "python": sys.version, "git_commit": commit.stdout.strip(),
        "harness_hashes": {
            name: hashlib.sha256((attempts.REPO_ROOT / "tools" / name).read_bytes()).hexdigest()
            for name in ("local_model_release_gate.py", "local_model_gate_attempts.py", "local_model_gate_recovery.py",
                         "local_model_gate_review.py", "local_model_gate_project_review.py")
        },
        "production_hashes": {
            path: hashlib.sha256((attempts.REPO_ROOT / path).read_bytes()).hexdigest()
            for path in ("codey/toolchain/constants.py", "codey/toolchain/definition.py", "codey/toolchain/runtime.py",
                         "codey/providers/api_provider.py", "codey/providers/api_chat.py",
                         "codey/providers/api_responses.py", "codey/providers/api_transport.py",
                         "codey/operations/kernel_transport.py",
                         "codey/app/event_payloads.py", "codey/app/event_bus.py", "codey/app/context.py",
                         "codey/app/headless_runner.py", "codey/app/task_services.py", "codey/task/entry_auth.py",
                         "codey/operations/kernel_recovery.py",
                         "codey/operations/project_adapter.py",
                         "codey/operations/task_loop.py", "codey/operations/kernel_prompt.py",
                         "codey/operations/task_guidance.py", "codey/research/completion_guidance.py",
                         "codey/research/tool_contract.py", "codey/reviews/report_sections.py",
                         "codey/operations/project_prompt_context.py", "codey/workspace/coding_context.py",
                         "codey/operations/planning_flow.py", "codey/operations/project_writer_phase.py",
                         "codey/operations/project_completion_enforcement.py", "codey/operations/task_execution.py",
                         "codey/research/tools.py", "codey/operations/project_completion_checks.py",
                         "codey/toolchain/tool_spec.py", "codey/operations/kernel_protocol.py",
                         "codey/research/connector_search.py", "codey/research/source_gateway.py",
                         "codey/reviews/core.py", "codey/reviews/input.py", "codey/reviews/identity.py",
                         "codey/reviews/persistence.py", "codey/reviews/reuse.py",
                         "codey/app/review_service.py", "codey/operations/project_review_phase.py",
                         "codey/runtime/core/operation_state.py")
        },
        "server_observations": _server_observations(target.base_url) if target.provider_id == "local" else {},
        "chat_template": "not_reported", "quantization": "model_name_only; not independently verified",
        "sampling_seed": "not_sent",
        "output_budget": {"configured": target.api_selection.get("output_tokens", "server_default"),
                          "actual_requests": "see per-case provider_metrics.output_budget"},
    }
    if target.provider_id == "zen":
        for path in ("codey/providers/zen/connection.py", "codey/providers/zen/declarations.py",
                     "codey/providers/zen/identity.py", "codey/providers/zen/catalog.py", "codey/providers/zen/access.py"):
            metadata["production_hashes"][path] = hashlib.sha256((attempts.REPO_ROOT / path).read_bytes()).hexdigest()
    return metadata


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument("--case", choices=(*CASES, "all"), default="all")
    selection.add_argument("--cases", help="comma-separated cases, run in the given order")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repeat", type=_positive_int, default=1)
    ap.add_argument("--timeout", type=_positive_int, default=600, help="whole-case wall deadline in seconds")
    ap.add_argument("--protocol", choices=("native", "json"), default="native")
    ap.add_argument("--model", default="", help="exact /models id; otherwise use its first id")
    ap.add_argument("--provider", choices=("local", "zen"), default="local")
    ap.add_argument("--turn-budget", type=_positive_int, default=0,
                    help="explicit per-task model turn budget; otherwise use scenario defaults")
    ap.add_argument("--run-dir", type=Path, help="new artifact directory; existing directories are rejected")
    ap.add_argument("--worker-case", choices=CASES, help=argparse.SUPPRESS)
    ap.add_argument("--worker-dir", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker_case:
        if args.worker_dir is None:
            ap.error("worker directory required")
        return _worker(args.worker_case, args.worker_dir.resolve())
    selected = args.cases.split(",") if args.cases else (list(DEFAULT_CASES) if args.case == "all" else [args.case])
    if any(case not in CASES for case in selected):
        ap.error("unknown case in --cases")
    if len(set(selected)) != len(selected):
        ap.error("duplicate case in --cases")
    directory = args.run_dir.resolve() if args.run_dir else attempts.create_run_dir(ARTIFACT_DIR)
    if args.run_dir:
        directory.mkdir(parents=True, exist_ok=False)
    _log(f"[gate] artifacts={directory}")
    try:
        from codey.providers.local_config import load_local_config, resolve_local_context_budget

        if args.provider == "local":
            base_url, models = probe_endpoint()
            model = args.model or (models[0] if models else "")
            if not model or model not in models:
                raise ValueError(f"selected model is not in /models: {model!r}")
            budget = resolve_local_context_budget(load_local_config())
            target = attempts.GateTarget(
                base_url, model, budget.context_window_tokens, budget.context_reserve_tokens,
                budget.context_keep_recent_tokens, protocol=args.protocol, request_timeout=min(args.timeout, TIMEOUT),
                api_protocol=load_local_config().api_protocol,
                turn_budget=args.turn_budget,
            )
        else:
            from codey.providers.api_connections import capture_selection
            from codey.providers.zen.identity import ZEN_BASE_URL

            if not args.model or args.protocol != "native":
                raise ValueError("Zen gate requires an explicit model and native tool protocol")
            admitted = capture_selection(args.provider, {"model": args.model})
            model = admitted.model_id
            target = attempts.GateTarget(ZEN_BASE_URL, model, admitted.context_window_tokens,
                admitted.context_reserve_tokens, admitted.context_keep_recent_tokens,
                provider_id=args.provider, api_selection=admitted.to_payload(), request_timeout=min(args.timeout, TIMEOUT),
                turn_budget=args.turn_budget)
        attempts.write_json(directory / "metadata.json", _metadata(target))
        _log(f"[gate] model={model} protocol={args.protocol} repeat={args.repeat}")
        results = attempts.run_attempts(selected, directory, target, repeat=args.repeat, timeout=args.timeout)
        payload = attempts.summarize(results, expected_cases=tuple(selected), repeat=args.repeat)
    except Exception as exc:
        payload = {"ok": False, "attempts": 0, "results": [], "failure_stage": "preflight_error",
                   "error": f"{type(exc).__name__}: {exc}"}
    payload["artifacts"] = str(directory)
    attempts.write_json(directory / "summary.json", payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in payload["results"]:
            print(f"{item['case']}: {'PASS' if item.get('ok') else 'FAIL'}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
