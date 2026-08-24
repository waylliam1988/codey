"""Deterministic validator for the frozen research benchmark suite.

This tool guards the 0.4.11 benchmark corpus under
``tests/fixtures/research_benchmark/``: split integrity, path containment,
regression-gate vocabulary alignment, rubric weights, and lock hashes. It is
offline by construction -- loading and validating never touches a provider.

``--update-lock`` is the one explicit escape hatch that rewrites ``lock.json``
after an intentional fixture change; every other invocation treats a hash
mismatch as a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.regression_gate import (
    CRITERION_NAMES,
    METRIC_NAMES,
    OBSERVABLE_NAMES,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "research_benchmark"
LOCK_NAME = "lock.json"
SUITE_NAME = "research_benchmark"
WEIGHT_TOLERANCE = 0.001
MAX_QUESTION_CHARS = 400
MAX_TAG_CHARS = 40
ALLOWED_CATEGORIES = frozenset({
    "industry_tracking",
    "paper_progress",
    "oss_ecosystem",
    "policy_change",
    "local_data_analysis",
    "injected_sources",
})
CASE_REQUIRED_KEYS = frozenset({"case_id", "category", "question", "expected_observables"})
CASE_ALLOWED_KEYS = CASE_REQUIRED_KEYS | {"title", "tags", "fixtures"}
# Case payloads describe tasks, never material: these key names are banned at
# every depth so raw prompt/transcript/source text cannot hide inside a case.
RAW_MATERIAL_KEYS = frozenset({"prompt", "reply", "transcript", "webpage", "excerpt"})


@dataclass(frozen=True)
class BenchmarkSuite:
    root: Path
    suite: dict[str, Any]
    rubric: dict[str, Any]
    lock: dict[str, Any]
    cases: dict[str, dict[str, Any]]

    def development_case_ids(self) -> tuple[str, ...]:
        return tuple(self.suite.get("development_cases") or ())

    def held_out_case_ids(self) -> tuple[str, ...]:
        return tuple(self.suite.get("held_out_cases") or ())

    def case_expectations(self, case_id: str) -> dict[str, bool]:
        payload = self.cases.get(case_id) or {}
        expectations = payload.get("expected_observables")
        return dict(expectations) if isinstance(expectations, Mapping) else {}


def load_suite(root: Path | None = None) -> BenchmarkSuite:
    """Load and JSON-parse the corpus. Structural validation is separate."""

    base = (root or FIXTURE_ROOT).resolve()
    suite = _read_json(base / "suite.json")
    rubric = _read_json(base / "rubric.json")
    lock = _read_json(base / LOCK_NAME)
    cases: dict[str, dict[str, Any]] = {}
    cases_dir = base / "cases"
    if cases_dir.is_dir():
        for path in sorted(cases_dir.glob("*.json")):
            cases[path.stem] = _read_json(path)
    return BenchmarkSuite(root=base, suite=suite, rubric=rubric, lock=lock, cases=cases)


def validate_suite(suite: BenchmarkSuite) -> tuple[str, ...]:
    """Return every structural error; an empty tuple means the corpus is sound."""

    errors: list[str] = []
    errors.extend(_validate_suite_manifest(suite))
    errors.extend(_validate_cases(suite))
    errors.extend(_validate_rubric(suite.rubric))
    errors.extend(_validate_lock(suite.root, suite.lock))
    return tuple(errors)


def update_lock(root: Path | None = None) -> Path:
    """Recompute and rewrite ``lock.json`` over every file except itself."""

    base = (root or FIXTURE_ROOT).resolve()
    entries = {
        _rel(base, path): _hash_file(path)
        for path in sorted(base.rglob("*"))
        if path.is_file() and path.name != LOCK_NAME
    }
    payload = {
        "lock_version": 1,
        "algorithm": "sha256",
        "entries": dict(sorted(entries.items())),
    }
    lock_path = base / LOCK_NAME
    tmp = lock_path.with_name(f".{LOCK_NAME}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(lock_path)
    return lock_path


def _validate_suite_manifest(suite: BenchmarkSuite) -> tuple[str, ...]:
    errors: list[str] = []
    manifest = suite.suite
    if manifest.get("suite") != SUITE_NAME:
        errors.append(f"suite name must be {SUITE_NAME!r}")
    version = manifest.get("version")
    if not isinstance(version, int) or version < 1:
        errors.append("suite.version must be a positive integer")
    dev = suite.development_case_ids()
    held = suite.held_out_case_ids()
    if not dev:
        errors.append("development_cases must not be empty")
    if not held:
        errors.append("held_out_cases must not be empty")
    overlap = sorted(set(dev) & set(held))
    if overlap:
        errors.append(f"development and held-out overlap: {overlap}")
    duplicate = sorted({name for names in (dev, held) for name in names if names.count(name) > 1})
    if duplicate:
        errors.append(f"duplicate case ids in splits: {duplicate}")
    referenced = set(dev) | set(held)
    on_disk = set(suite.cases)
    missing = sorted(referenced - on_disk)
    orphaned = sorted(on_disk - referenced)
    if missing:
        errors.append(f"splits reference missing case files: {missing}")
    if orphaned:
        errors.append(f"case files not referenced by any split: {orphaned}")
    return tuple(errors)


def _validate_cases(suite: BenchmarkSuite) -> tuple[str, ...]:
    errors: list[str] = []
    for case_id, payload in sorted(suite.cases.items()):
        prefix = f"case {case_id}"
        if not isinstance(payload, dict):
            errors.append(f"{prefix}: payload must be an object")
            continue
        keys = set(payload)
        missing_keys = sorted(CASE_REQUIRED_KEYS - keys)
        unknown_keys = sorted(keys - CASE_ALLOWED_KEYS)
        if missing_keys:
            errors.append(f"{prefix}: missing keys {missing_keys}")
        if unknown_keys:
            errors.append(f"{prefix}: unknown keys {unknown_keys}")
        if payload.get("case_id") != case_id:
            errors.append(f"{prefix}: case_id does not match file name")
        if payload.get("category") not in ALLOWED_CATEGORIES:
            errors.append(f"{prefix}: unknown category {payload.get('category')!r}")
        for field in ("title", "question"):
            value = payload.get(field)
            if field in keys and (not isinstance(value, str) or not value.strip()):
                errors.append(f"{prefix}: {field} must be a non-empty string")
        question = payload.get("question")
        if isinstance(question, str) and len(question) > MAX_QUESTION_CHARS:
            errors.append(f"{prefix}: question exceeds {MAX_QUESTION_CHARS} chars")
        tags = payload.get("tags", [])
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not tag or len(tag) > MAX_TAG_CHARS for tag in tags
        ):
            errors.append(f"{prefix}: tags must be short strings")
        fixtures = payload.get("fixtures", [])
        if not isinstance(fixtures, list):
            errors.append(f"{prefix}: fixtures must be a list")
        else:
            for rel in fixtures:
                problem = _fixture_problem(suite.root, rel)
                if problem:
                    errors.append(f"{prefix}: {problem}")
        expectations = payload.get("expected_observables")
        if not isinstance(expectations, dict) or not expectations:
            errors.append(f"{prefix}: expected_observables must be a non-empty object")
        else:
            for name, expected in expectations.items():
                if name not in OBSERVABLE_NAMES:
                    errors.append(f"{prefix}: unknown expected observable {name!r}")
                if not isinstance(expected, bool):
                    errors.append(f"{prefix}: expectation {name!r} must be boolean")
        for bad_key in _find_raw_material_keys(payload):
            errors.append(f"{prefix}: raw-material key {bad_key!r} is banned")
    return tuple(errors)


def _validate_rubric(rubric: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    metrics = rubric.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return ("rubric.metrics must be a non-empty list",)
    total_weight = 0.0
    seen_names: set[str] = set()
    for index, row in enumerate(metrics):
        prefix = f"rubric metric #{index}"
        if not isinstance(row, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{prefix}: missing name")
        elif name in seen_names:
            errors.append(f"{prefix}: duplicate name {name!r}")
        else:
            seen_names.add(name)
        weight = row.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            errors.append(f"{prefix}: weight must be a positive number")
        else:
            total_weight += float(weight)
        references = [
            key
            for key in ("observable", "negated_observable", "metric")
            if key in row
        ]
        if len(references) != 1:
            errors.append(f"{prefix}: exactly one of observable/negated_observable/metric")
            continue
        kind = references[0]
        target = row[kind]
        vocabulary = {
            "observable": OBSERVABLE_NAMES,
            "negated_observable": OBSERVABLE_NAMES,
            "metric": METRIC_NAMES,
        }[kind]
        if target not in vocabulary:
            errors.append(f"{prefix}: unknown {kind} {target!r}")
    if abs(total_weight - 1.0) > WEIGHT_TOLERANCE:
        errors.append(f"rubric weights must sum to 1.0 (got {round(total_weight, 4)})")
    hard_gates = rubric.get("hard_gates", [])
    if not isinstance(hard_gates, list):
        errors.append("rubric.hard_gates must be a list")
    else:
        for gate in hard_gates:
            if gate not in CRITERION_NAMES:
                errors.append(f"rubric hard gate {gate!r} is not a gate criterion")
    return tuple(errors)


def _validate_lock(root: Path, lock: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    entries = lock.get("entries")
    if not isinstance(entries, dict):
        return ("lock.entries must be an object",)
    if lock.get("algorithm") != "sha256":
        errors.append("lock.algorithm must be sha256")
    actual = {
        _rel(root, path): _hash_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != LOCK_NAME
    }
    recorded = {str(key): str(value) for key, value in entries.items()}
    missing = sorted(set(actual) - set(recorded))
    extra = sorted(set(recorded) - set(actual))
    if missing:
        errors.append(f"lock is missing hashes for: {missing}")
    if extra:
        errors.append(f"lock hashes files that no longer exist: {extra}")
    for rel in sorted(set(actual) & set(recorded)):
        if actual[rel] != recorded[rel]:
            errors.append(f"hash mismatch for {rel}")
    return tuple(errors)


def _fixture_problem(root: Path, rel: object) -> str:
    if not isinstance(rel, str) or not rel.strip():
        return "fixture paths must be non-empty strings"
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return f"fixture escapes suite root: {rel}"
    if not candidate.is_file():
        return f"fixture missing: {rel}"
    return ""


def _find_raw_material_keys(payload: Mapping[str, Any], prefix: str = "") -> list[str]:
    found: list[str] = []
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if str(key).lower() in RAW_MATERIAL_KEYS:
            found.append(path)
        if isinstance(value, Mapping):
            found.extend(_find_raw_material_keys(value, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    found.extend(_find_raw_material_keys(item, f"{path}[{index}]"))
    return found


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _hash_file(path: Path) -> str:
    """Hash file content stably across CRLF/LF working-tree differences.

    Git normalizes text files to LF (``* text=auto eol=lf``), so digests are
    computed over LF-normalized bytes when the file is valid UTF-8 text and
    over raw bytes otherwise (the fixture PDF stays byte-exact).
    """

    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "sha256:" + hashlib.sha256(data).hexdigest()
    return "sha256:" + hashlib.sha256(
        text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the frozen research benchmark suite (offline).",
    )
    parser.add_argument("--root", type=Path, default=FIXTURE_ROOT)
    parser.add_argument(
        "--update-lock",
        action="store_true",
        help="Recompute lock.json after an intentional fixture change.",
    )
    args = parser.parse_args(argv)
    if args.update_lock:
        lock_path = update_lock(args.root)
        print(f"lock updated: {lock_path}")
    suite = load_suite(args.root)
    errors = validate_suite(suite)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        f"suite ok: {len(suite.development_case_ids())} development, "
        f"{len(suite.held_out_case_ids())} held-out cases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
