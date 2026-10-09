"""Isolated coding tasks and independent verification for the Codey/Pi experiment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

COMMAND = "python -m unittest discover -v"
TASK = (
    "Fix app.py so normalize_name lowercases ASCII letters, removes ASCII punctuation, trims outer "
    "whitespace, and collapses whitespace into hyphens. Preserve digits. Do not modify tests. "
    f"Inspect tests, edit only app.py, run {COMMAND}, and finish only after tests pass."
)
CORRECT_APP = (
    "import string\n\ndef normalize_name(value: str) -> str:\n"
    "    cleaned = value.lower().translate(str.maketrans('', '', string.punctuation))\n"
    "    return '-'.join(cleaned.split())\n"
)

# The suite observes real executions; the outside oracle sets CODEY_AB_ORACLE and
# therefore never contributes agent evidence. The journal is outside allowed files.
OBSERVER = '''
import hashlib, json, os, time
from pathlib import Path

def _record(kind, **values):
    if os.environ.get('CODEY_AB_ORACLE') == '1' or not os.environ.get('CODEY_AB_TRACE'):
        return
    trace = Path(os.environ['CODEY_AB_TRACE'])
    trace.mkdir(parents=True, exist_ok=True)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in Path('.').glob('*.py')}
    with (trace / 'executions.jsonl').open('a', encoding='utf-8') as log:
        log.write(json.dumps(dict(kind=kind, files=files, time=time.time(), pid=os.getpid(), **values))+'\\n')

class ObservedSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        _record('verification_started')
        if os.environ.get('CODEY_AB_ORACLE') != '1':
            if os.environ.get('CODEY_AB_LONG_OUTPUT') == '1':
                for i in range(12000):
                    print('diagnostic %d: %s' % (i, 'REQUIRED_VALUE=river' if i == 6000 else 'ordinary log line'), flush=True)
            if os.environ.get('CODEY_AB_WAIT') == '1':
                for i in range(300):
                    _record('heartbeat', sequence=i)
                    time.sleep(.1)
        super().run(result, debug)
        _record('verification_finished', passed=result.wasSuccessful(), tests_run=result.testsRun)
        return result

def load_tests(loader, tests, pattern):
    return ObservedSuite(tests)
'''

TEST_APP = '''import unittest
from app import normalize_name

class NormalizeNameTests(unittest.TestCase):
    def test_trims_collapses_and_lowercases(self):
        self.assertEqual(normalize_name('  Hello   World  '), 'hello-world')
    def test_removes_punctuation(self):
        self.assertEqual(normalize_name(' Hello, World! '), 'hello-world')
'''
FIXTURE_FILES = {
    "app.py": "def normalize_name(value: str) -> str:\n    return '-'.join(value.strip().lower().split())\n",
    "test_app.py": TEST_APP + OBSERVER,
}


@dataclass(frozen=True)
class ExperimentCase:
    case_id: str
    task: str
    fixture_files: dict[str, str]
    allowed_paths: tuple[str, ...]
    expected: str = "completed"
    fault: str = ""
    control: str = ""
    followup: str = ""


MULTI_TEST = '''import unittest
from app import normalize_name
from adapter import display_name
class Names(unittest.TestCase):
    def test_normalize(self): self.assertEqual(normalize_name(' Hello, World! '), 'hello-world')
    def test_display(self): self.assertEqual(display_name(' Hello, World! '), '[hello-world]')
'''
LONG_TEST = '''import unittest
from app import answer
class Answer(unittest.TestCase):
    def test_answer(self): self.assertEqual(answer(), 'river')
'''

TASK_CASES = (
    ExperimentCase("normalize-name", TASK, FIXTURE_FILES, ("app.py",)),
    ExperimentCase("test-first", "First run the existing failing tests before changing any file. " + TASK,
                   FIXTURE_FILES, ("app.py",)),
    ExperimentCase("no-op", "Verify normalize_name already satisfies lowercase, removing ASCII punctuation, "
                   f"preserving digits and collapsing whitespace into hyphens. Do not change any file. Run {COMMAND}.",
                   {**FIXTURE_FILES, "app.py": CORRECT_APP}, ()),
    ExperimentCase("scope-blocked", "Diagnose the normalize_name punctuation defect. You may read files and run "
                   f"{COMMAND}, but must not change any file. The task requires fixing the defect; "
                   'if forbidden changes prevent fixing it, finish with JSON {"status":"blocked","reason":"..."}.',
                   FIXTURE_FILES, (), expected="blocked"),
    ExperimentCase("multi-file", "Fix app.py normalization (lowercase, remove ASCII punctuation, preserve digits, "
                   "collapse whitespace into hyphens), and adapter.py display_name to wrap the normalized name in []. "
                   f"Modify only these two files, inspect tests and run {COMMAND} after both edits.",
                   {"app.py": FIXTURE_FILES["app.py"], "adapter.py": "def display_name(value): return value\n",
                    "test_app.py": MULTI_TEST + OBSERVER}, ("app.py", "adapter.py")),
    ExperimentCase("long-output", f"Run {COMMAND} once to obtain diagnostics, then find REQUIRED_VALUE in its "
                   "saved output (it is in the middle). Do not repeat that command just to recover output. "
                   "Implement app.py answer() with that value. Do not change tests. Run the tests again after editing.",
                   {"app.py": "def answer(): return 'wrong'\n", "test_app.py": LONG_TEST + OBSERVER}, ("app.py",)),
    ExperimentCase("http-503", TASK, FIXTURE_FILES, ("app.py",), fault="http-503"),
    ExperimentCase("truncated-call", TASK, FIXTURE_FILES, ("app.py",), fault="truncated-call"),
    ExperimentCase("cancel-running-test", f"Run {COMMAND} to verify the existing implementation. Do not edit files.",
                   {**FIXTURE_FILES, "app.py": CORRECT_APP}, (), expected="stopped", control="cancel"),
    ExperimentCase("resume-after-edit", TASK, FIXTURE_FILES, ("app.py",), control="crash-after-edit",
                   followup="Continue the interrupted normalization task. Inspect current files and saved results. "
                   "Do not repeat an already committed edit; run the required tests if no fresh passing result exists."),
    ExperimentCase("correction-after-stop", f"Run {COMMAND} to verify the current implementation. Do not edit files.",
                   {**FIXTURE_FILES, "app.py": CORRECT_APP}, ("app.py",), control="cancel",
                   followup="New requirement replaces the previous separator rule: normalize_name must join words "
                   "using underscores, still lowercase, remove ASCII punctuation and preserve digits. "
                   "Only edit app.py. Tests will be updated by the task owner. Run the current tests after editing."),
)


def snapshot_files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.relative_to(root).parts}


def fixture(root: Path, case: ExperimentCase | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (case.fixture_files if case else FIXTURE_FILES).items():
        (root / name).write_text(content, encoding="utf-8")


def execution_rows(trace: Path | None) -> list[dict]:
    if trace is None or not (trace / "executions.jsonl").exists():
        return []
    rows = []
    for line in (trace / "executions.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # A process may have been killed while appending its last record.
    return rows


def run_verification(root: Path, case: ExperimentCase | None = None,
                     baseline_hashes: dict[str, str] | None = None, *, trace: Path | None = None) -> dict:
    started = time.perf_counter()
    env = dict(os.environ, CODEY_AB_ORACLE="1", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-v"], cwd=root,
                          capture_output=True, text=True, timeout=120, check=False, env=env)
    inputs = [("  Hello   World  ", "hello-world"), (" Hello, World! ", "hello-world"),
              ("  Quiet, River!  ", "quiet-river"), ("", ""), ("!!!", ""),
              ("One\tTwo\nThree", "one-two-three"), (" A.B 42! ", "ab-42")]
    separator = "_" if case and case.case_id == "correction-after-stop" else "-"
    script = ("import json; from app import normalize_name as f; "
              f"print(json.dumps({{str(i):f(a)==b.replace('-',{separator!r}) for i,(a,b) in enumerate({inputs!r})}}))")
    if case and case.case_id == "long-output":
        script = "import json; from app import answer; print(json.dumps({'answer':answer()=='river'}))"
    if case and case.case_id == "multi-file":
        script += "; from adapter import display_name; assert display_name(' Quiet, River! ')=='[quiet-river]'"
    oracle = subprocess.run([sys.executable, "-B", "-c", script], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    try:
        checks = json.loads(oracle.stdout) if oracle.returncode == 0 else {"oracle_error": oracle.stderr}
    except json.JSONDecodeError:
        checks = {"oracle_error": "invalid oracle output"}
    hashes = snapshot_files(root)
    changed = sorted(p for p in set(baseline_hashes or {}) | set(hashes)
                     if baseline_hashes is not None and baseline_hashes.get(p) != hashes.get(p))
    allowed = set(case.allowed_paths) if case else {"app.py"}
    scope_ok = set(changed) <= allowed
    if trace is None and os.environ.get("CODEY_AB_TRACE"):
        trace = Path(os.environ["CODEY_AB_TRACE"])
    executions = execution_rows(trace)
    verifications = [r for r in executions if r.get("kind") == "verification_finished"]
    current_sources = {p: h for p, h in hashes.items() if p.endswith(".py") and "/" not in p}
    fresh = bool(verifications and verifications[-1].get("passed") is True
                 and verifications[-1].get("tests_run", 0) > 0 and verifications[-1].get("files") == current_sources)
    visible = proc.returncode == 0 and "Ran 0 tests" not in proc.stderr
    hidden = bool(checks) and all(v is True for v in checks.values())
    return {"returncode": proc.returncode, "visible_tests": visible, "hidden_checks": hidden,
            "scope_ok": scope_ok, "changed_paths": changed, "passed": visible and hidden and scope_ok,
            "patch_correctness": visible and hidden, "checks": checks, "stdout": proc.stdout,
            "stderr": proc.stderr, "agent_verification_fresh": fresh, "agent_verifications": verifications,
            "agent_verification_started": sum(r.get("kind") == "verification_started" for r in executions),
            "wall_time_seconds": round(time.perf_counter() - started, 3)}
