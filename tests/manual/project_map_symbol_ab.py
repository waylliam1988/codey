from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.project_map import (
    MAX_SYMBOL_FILE_BYTES,
    MAX_SYMBOL_MAP_CHARS,
    build_symbol_overview,
    render_project_map,
)
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.tool_runtime import list_directory


@dataclass(frozen=True)
class ProbeCase:
    name: str
    task: str
    expected_paths: tuple[str, ...]


CASES = (
    ProbeCase(
        name="tool-command-allowlist",
        task=(
            "I want to adjust how Codey proposes verification commands and test candidates. "
            "Where should the implementation and focused tests likely be changed first?"
        ),
        expected_paths=("codey/verification_map.py", "tests/test_verification_map.py"),
    ),
    ProbeCase(
        name="json-parallel-protocol",
        task=(
            "I need to change how Codey validates parallel and read_files JSON tool calls. "
            "Which files should be inspected first?"
        ),
        expected_paths=("codey/protocols/json_codec.py", "tests/test_protocols.py"),
    ),
    ProbeCase(
        name="snapshot-restore",
        task=(
            "I need to investigate restore conflicts for non-git projects and snapshot diffs. "
            "Which files are the best starting points?"
        ),
        expected_paths=("codey/changes.py", "tests/test_changes.py"),
    ),
    ProbeCase(
        name="provider-control-recovery",
        task=(
            "I want to improve recovery when a provider web page changes its message box "
            "or send button selectors. Which files should be read first?"
        ),
        expected_paths=("codey/provider_controls.py", "codey/provider_discovery.py"),
    ),
    ProbeCase(
        name="task-run-state",
        task=(
            "I need to debug how Codey reserves a run, emits SSE task events, and finishes "
            "a project task. Which files are likely involved?"
        ),
        expected_paths=("codey/server.py", "codey/task_runner.py", "tests/test_server.py"),
    ),
)


def build_project_symbol_map(
    root: Path,
    task: str,
    *,
    max_chars: int = MAX_SYMBOL_MAP_CHARS,
) -> str:
    return build_symbol_overview(root, task, max_chars=max_chars)


def _prompt(case: ProbeCase, root: Path, *, arm: str) -> str:
    listing = list_directory(root, ".").output
    parts = [
        "You are helping evaluate a local coding agent's project navigation.",
        "Do not solve the code change. Only choose files to inspect first.",
        "Return exactly one JSON object, no markdown:",
        '{"paths":["relative/path.py","relative/test_path.py"],"reason":"short reason"}',
        "",
        f"Project root: {root}",
        "Initial listing:",
        listing,
    ]
    if arm in {"project_map", "symbol_map"}:
        parts.extend(["", render_project_map(root, task=case.task if arm == "symbol_map" else "")])
    parts.extend(["", "Task:", case.task])
    return "\n".join(parts)


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    raw = text[start : index + 1]
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else {}
        start = text.find("{", start + 1)
    return {}


def _paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    raw_paths = obj.get("paths")
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list):
        return ()
    paths = []
    for item in raw_paths:
        value = str(item or "").replace("\\", "/").strip()
        if value and value not in paths:
            paths.append(value)
    return tuple(paths)


def _score_paths(paths: tuple[str, ...], expected: tuple[str, ...]) -> dict[str, Any]:
    expected_set = set(expected)
    path_set = set(paths)
    hits = tuple(path for path in expected if path in path_set)
    first_expected_rank = None
    for index, path in enumerate(paths, start=1):
        if path in expected_set:
            first_expected_rank = index
            break
    return {
        "hits": list(hits),
        "hit_count": len(hits),
        "first_expected_rank": first_expected_rank,
        "top1_hit": bool(paths and paths[0] in expected_set),
    }


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / ".hidden").mkdir()
        (root / "src" / "router.py").write_text(
            "SECRET_BODY = 'not a real secret, but should not be body proof'\n\n"
            "class Router:\n"
            "    def dispatch(self, request, context):\n"
            "        return 'BODY_SHOULD_NOT_APPEAR'\n\n"
            "def build_router(config):\n"
            "    return Router()\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_router.py").write_text(
            "def test_router_dispatch():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        (root / ".hidden" / "hidden.py").write_text(
            "def hidden_symbol():\n    pass\n",
            encoding="utf-8",
        )
        (root / "token_helper.py").write_text(
            "def token_symbol():\n    pass\n",
            encoding="utf-8",
        )
        (root / "src" / "big.py").write_text(
            "def big_symbol():\n    pass\n" + ("#" * (MAX_SYMBOL_FILE_BYTES + 1)),
            encoding="utf-8",
        )
        try:
            (root / "src" / "link.py").symlink_to(root / "src" / "router.py")
        except OSError:
            pass
        text = build_symbol_overview(root, "debug router dispatch test")
        assert "src/router.py" in text, text
        assert "class Router" in text, text
        assert "def Router.dispatch(self, request, context)" in text, text
        assert "tests/test_router.py" in text, text
        assert "BODY_SHOULD_NOT_APPEAR" not in text, text
        assert "return Router" not in text, text
        assert "hidden_symbol" not in text, text
        assert "token_symbol" not in text, text
        assert "big_symbol" not in text, text
        assert "link.py" not in text, text
        assert len(text) <= MAX_SYMBOL_MAP_CHARS + 120, len(text)
    print("self-test passed")


ARMS = ("baseline", "project_map", "symbol_map")


def run_probe(root: Path, provider_id: str, port: int, cases: tuple[ProbeCase, ...]) -> dict[str, Any]:
    results = []
    provider_controls.begin_task_context(f"project-map-symbol-ab:{provider_id}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        for case in cases:
            case_result: dict[str, Any] = {
                "case": case.name,
                "expected_paths": list(case.expected_paths),
            }
            for label in ARMS:
                provider.new_chat()
                reply = provider.send(_prompt(case, root, arm=label), timeout=180)
                paths = _paths_from_reply(reply)
                case_result[label] = {
                    "paths": list(paths),
                    "score": _score_paths(paths, case.expected_paths),
                    "raw_reply": reply[:2000],
                }
                print(f"[{case.name}] {label}: {', '.join(paths) or '(no paths)'}")
            results.append(case_result)
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()

    hits = {
        arm: sum(item[arm]["score"]["hit_count"] for item in results)
        for arm in ARMS
    }
    top1 = {
        arm: sum(1 for item in results if item[arm]["score"]["top1_hit"])
        for arm in ARMS
    }
    return {
        "provider": provider_id,
        "cases": results,
        "summary": {
            "case_count": len(results),
            "hit_count": hits,
            "top1_hits": top1,
            "symbol_vs_project_map_hit_delta": hits["symbol_map"] - hits["project_map"],
            "symbol_vs_project_map_top1_delta": top1["symbol_map"] - top1["project_map"],
            "symbol_vs_baseline_hit_delta": hits["symbol_map"] - hits["baseline"],
            "symbol_vs_baseline_top1_delta": top1["symbol_map"] - top1["baseline"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Project Map and task-aware Symbol overview for first-file selection."
    )
    parser.add_argument("--project", default=".", help="Project root to probe.")
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", choices=[case.name for case in CASES], action="append")
    parser.add_argument(
        "--print-map",
        action="store_true",
        help="Only print the task-aware symbol overview for the first selected case.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run local symbol overview invariants only.")
    parser.add_argument("--out", default="", help="Optional JSON report path.")
    args = parser.parse_args(argv)

    root = Path(args.project).expanduser().resolve()
    if args.self_test:
        run_self_test()
        return 0
    selected = tuple(case for case in CASES if not args.case or case.name in args.case)
    if args.print_map:
        case = selected[0]
        print(build_project_symbol_map(root, case.task))
        return 0

    report = run_probe(root, args.provider, args.port, selected)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
