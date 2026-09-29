"""Local-model release gate for Codey (auto-captured, no human watch needed).

Runs chat + agent (create/edit/references/hybrid/discussion/planning/auto) + ghost
against a local OpenAI-compatible endpoint (KoboldCpp is the default
http://127.0.0.1:5001/v1), captures headless JSONL per case into
.e2e-artifacts/local-model-release-<case>.jsonl, and verifies independently of the
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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV
from codey.providers.local_discovery import LOCAL_BASE_URL_CANDIDATES, probe_local_endpoint_detail

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / ".e2e-artifacts"
DEFAULT_BASE_URLS = LOCAL_BASE_URL_CANDIDATES
PROVIDER_ID = "local"
TIMEOUT = 600.0

CASES = (
    "chat", "create", "edit", "references", "hybrid", "discussion", "planning", "auto", "ghost",
)


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
    if case in {"create", "discussion", "planning", "auto"}:
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
    if case == "create":
        return (
            "Create math_utils.py with add(a, b) returning a + b. "
            "Create tests/test_math_utils.py with a unittest for add(2, 3) == 5. "
            "Use edit with content to create the files, then run python -m unittest discover "
            "and finish with done.",
            "project",
            10,
        )
    if case == "edit":
        return (
            "Fix the LIVE_SMOKE_BUG in pricing.py by using search, read, edit, "
            "then run python -m unittest discover and finish with done.",
            "project",
            10,
        )
    if case == "references":
        return (
            "Update calculate_total(amount, tax_rate, discount=0) so it subtracts "
            "discount before tax, i.e. (amount - discount) * (1 + tax_rate). "
            "Update all callers that should use the discount. Before editing, use "
            "find_references for calculate_total, then read_file for the real source "
            "near each affected location. Run python -m unittest discover and finish.",
            "project",
            12,
        )
    if case == "hybrid":
        return (
            "Use the shared hybrid task path. First web_search for the pricing "
            "discount context, then open_url/open_result the relevant source, "
            "then read_file pricing.py, fix LIVE_SMOKE_BUG with edit, "
            "run python -m unittest discover, and finish with done. "
            "Do the steps in order: search -> open -> read -> edit -> verify -> done.",
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


def _verify_fixture(root: Path, case: str) -> dict:
    if case == "discussion" or case == "planning":
        files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        return {
            "ok": not files,
            "exit_code": 0 if not files else 1,
            "output": "no files changed" if not files else f"unexpected files: {files}",
        }
    if case == "create":
        assertion = "from math_utils import add; assert add(2, 3) == 5"
    elif case in {"edit", "hybrid"}:
        assertion = "from pricing import discounted_price; assert discounted_price(100, 20) == 80"
    elif case == "references":
        assertion = (
            "from checkout import checkout_total, discounted_checkout_total; "
            "from pricing import calculate_total; "
            "assert checkout_total() == 120; "
            "assert calculate_total(100, 0.2, discount=10) == 108; "
            "assert discounted_checkout_total() == 108"
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
    commands = (
        [sys.executable, "-B", "-c", assertion],
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
    return {"ok": True, "exit_code": 0, "output": combined}


def run_chat_case() -> dict:
    from codey.providers.local_openai import LocalOpenAIProvider

    base_url, models = probe_endpoint()
    model = models[0] if models else "local-model"
    provider = LocalOpenAIProvider(base_url, model, timeout=TIMEOUT)
    t0 = time.time()
    try:
        reply = provider.send("Reply with exactly: KOBOLD_OK", timeout=TIMEOUT)
    finally:
        provider.close()
    dt = round(time.time() - t0, 1)
    ok = "KOBOLD_OK" in str(reply)
    return {
        "case": "chat", "ok": ok, "seconds": dt,
        "base_url": base_url, "model": model,
        "reply_preview": str(reply)[:500],
    }


def run_agent_case(case: str) -> dict:
    task, intent, max_turns = _task_for(case)
    root = Path(tempfile.mkdtemp(prefix=f"codey-kobold-gate-{case}-")).resolve()
    # Isolated state: live runs must never pollute the user's default Ghost
    # state, and Ghost assertions below observe this run's own records.
    state_home = Path(tempfile.mkdtemp(prefix=f"codey-kobold-gate-state-{case}-")).resolve()
    rows: list[dict] = []
    try:
        _make_fixture(root, case)
        request = HeadlessRequest(
            project=root, task=task, provider_id=PROVIDER_ID,
            max_turns=max_turns, intent=intent, state_home=state_home,
        )
        t0 = time.time()
        result = run_headless(request, emit_jsonl=rows.append)
        dt = round(time.time() - t0, 1)
        done = next((r for r in reversed(rows) if str(r.get("type") or "") == "task_done"), None)
        verification = _verify_fixture(root, case)
        stop_reason = str((done or {}).get("stop_reason") or result.stop_reason)
        tool_names = _tool_names_in_order(rows)
        single_session = check_single_session_identity(rows)
        # Hybrid additionally proves ordered tool use on one session:
        # search -> open -> read -> edit -> run -> done. Other cases prove
        # files+done+independent verification only (see module docstring).
        hybrid_order: dict | None = None
        if case == "hybrid":
            hybrid_order = check_hybrid_tool_order(rows)
        ok = (
            stop_reason == "done"
            and result.exit_code == 0
            and done is not None
            and verification["ok"]
            and single_session["ok"]
            and (hybrid_order is None or bool(hybrid_order.get("ok")))
        )
        data = {
            "case": case, "ok": ok, "seconds": dt,
            "stop_reason": stop_reason, "turns": int((done or {}).get("turns") or 0),
            "exit_code": result.exit_code,
            "summary": str((done or {}).get("summary") or "")[:1000],
            "verification": verification,
            "project": str(root),
            "run_id": result.run_id, "session_id": result.session_id,
            "jsonl_rows": len(rows),
            "tool_names": tool_names,
            "tool_order": hybrid_order,
            "single_session": single_session,
        }
    except Exception as exc:  # noqa: BLE001 - gate must report, not raise
        data = {"case": case, "ok": False, "error": f"{type(exc).__name__}: {exc}", "project": str(root)}
    finally:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(ARTIFACT_DIR / f"local-model-release-{case}.jsonl", "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            data = dict(data)
            data["ok"] = False
            data["artifact_error"] = f"could not archive JSONL: {exc}"
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(state_home, ignore_errors=True)
    return data


def run_ghost_case() -> dict:
    """Ghost control-plane roundtrip inside an isolated state home.

    Writes one observation for this run, reads it back by run_id, checks
    retrieval surfaces it, deletes the session scope, and asserts it is
    no longer retrievable. Also asserts the default user state is reachable
    (list/export) without mutating it.
    """
    from codey.ghost.control_surface import GhostControlSurface
    from codey.ghost.observation_index import retrieve_relevant_observations
    from codey.ghost.observations import GhostObservationStore
    from codey.storage.local_store import DEFAULT_STATE_HOME

    default_surface = GhostControlSurface.from_state_home(DEFAULT_STATE_HOME)
    if not default_surface.available or default_surface.inbox is None:
        return {"case": "ghost", "ok": False, "error": "default ghost store unavailable"}
    try:
        listed = default_surface.inbox.list_candidates()
        exported = default_surface.export_state()
        default_ok = bool(exported.get("ok"))
    except Exception as exc:  # noqa: BLE001
        return {"case": "ghost", "ok": False, "error": f"default state read failed: {exc}"}
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
            "case": "ghost", "ok": default_ok,
            "candidates": len(listed),
            "observations": len(((exported or {}).get("observations") or {}).get("observations") or []),
            "learning_enabled": default_surface.inbox.learning_enabled(),
            "roundtrip": "write->read->retrieve->delete->gone",
        }
    except Exception as exc:  # noqa: BLE001
        return {"case": "ghost", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(state_home, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=(*CASES, "all"), default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    selected = list(CASES) if args.case == "all" else [args.case]
    base_url, models = probe_endpoint()
    _log(f"[gate] local model {base_url} models={list(models)[:3]}")
    results: list[dict] = []
    for case in selected:
        _log(f"[gate] case={case} ...")
        if case == "chat":
            data = run_chat_case()
        elif case == "ghost":
            data = run_ghost_case()
        else:
            data = run_agent_case(case)
        data.setdefault("case", case)
        results.append(data)
        _log(f"[gate] case={case} {'PASS' if data.get('ok') else 'FAIL'} {json.dumps(data, ensure_ascii=False)[:1000]}")

    payload = {"ok": all(r.get("ok") for r in results), "base_url": base_url, "results": results}
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARTIFACT_DIR / "local-model-release-summary.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in results:
            print(f"{item['case']}: {'PASS' if item.get('ok') else 'FAIL'}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
