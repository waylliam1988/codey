"""Same local-model tasks against the original Codey, Pi, OpenCode and DSH CLIs.

The HTTP proxy observes and budgets requests; it never repairs arguments or
answers. All workspaces/state are isolated. Raw events and requests are kept.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import yaml

from tests.manual.agent_stability_cases import TASK_CASES, execution_rows
from tests.manual.agent_stability_measurements import usage_totals
from tests.manual.agent_stability_proxy import Proxy
from tests.manual.codey_vs_pi_agent_stability_ab import (
    ROOT,
    _pi_command,
    _run_process,
    _sampling_extension,
    _wait_for_backend_idle,
    _write_pi_config,
)
from tests.manual.opencode_installed_agent_worker import local_config

MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
CASES = {
    "quoted": {
        "files": {"config.py": 'before\nvalue = "is true and false or null"\nafter\n'},
        "task": 'Read config.py using tools. Return only JSON {"line":2,"text":<the complete exact second line without its newline>}. Do not change files.',
        "expected": {"line": 2, "text": 'value = "is true and false or null"'},
    },
    "glob": {
        "files": {
            "src/a.py": 'TAG.*"Q"\n',
            "src/b.ts": 'TAG.*"Q"\n',
            "src/no.txt": 'TAG.*"Q"\n',
            "src/vendor/c.py": 'TAG.*"Q"\n',
            "other/d.py": 'TAG.*"Q"\n',
        },
        "task": 'Search only src for the literal text TAG.*"Q" in .py and .ts files, excluding src/vendor and all its descendants. Use tools to inspect the workspace. Return only JSON {"paths":[<matching paths relative to the workspace root, including the src/ prefix, sorted alphabetically>]}. Do not change files.',
        "expected": {"paths": ["src/a.py", "src/b.ts"]},
    },
    "context": {
        "files": {"lines.txt": "before1\nneedle\nafter1\nafter2\nbefore2\nneedle\nafter3\nafter4\nend\n"},
        "task": 'Inspect lines.txt using tools. Find the second line containing needle. Return only JSON {"line":<its 1-based line number>,"after":[<the exact two lines immediately after it, without newlines>]}. Do not include lines before it. Do not change files.',
        "expected": {"line": 6, "after": ["after3", "after4"]},
    },
}
EDIT = next(c for c in TASK_CASES if c.case_id == "multi-file")
CASES["multi-file"] = {"files": EDIT.fixture_files, "task": EDIT.task, "expected": None}


def final_text(arm, rows):
    if arm == "codey":
        return next((r.get("summary", "") for r in reversed(rows) if r.get("type") == "task_done"), "")
    if arm == "pi":
        message = next(
            (
                r["message"]
                for r in reversed(rows)
                if r.get("type") == "message_end" and r.get("message", {}).get("role") == "assistant"
            ),
            {},
        )
        return "\n".join(c.get("text", "") for c in message.get("content", []) if c.get("type") == "text")
    if arm == "dsh":
        return next((r.get("text", "") for r in reversed(rows) if r.get("type") == "final"), "")
    return "".join(r.get("part", {}).get("text", "") for r in rows if r.get("type") == "text")


def command(arm, directory, project, url, env, output_tokens):
    node = shutil.which("node")
    if arm == "codey":
        return [
            sys.executable,
            "-B",
            "-m",
            "tests.manual.agent_stability_codey_worker",
            "--project",
            str(project),
            "--state-home",
            str(directory / "state"),
            "--session-id",
            "comparison",
            "--max-turns",
            "16",
        ]
    if arm == "pi":
        config = directory / "pi-config"
        _write_pi_config(config, url, MODEL, max_tokens=output_tokens, window=32768)
        env["PI_CODING_AGENT_DIR"] = str(config)
        extension = _sampling_extension(directory, 41, 0)
        return _pi_command(ROOT / "reference-projects/pi", node, "source", directory / "unused-resources", env) + [
            "--provider",
            "kobold",
            "--model",
            MODEL,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "-e",
            str(extension),
            "--session",
            str(directory / "session.jsonl"),
            "--mode",
            "json",
            "--print",
        ]
    if arm == "opencode":
        root = ROOT / "reference-projects/opencode/packages/cli"
        # The wrapper changes only cwd; the repository's Bun preload and JSX
        # configuration remain active for both the CLI and its private server.
        wrapper = directory / "opencode-entry.ts"
        wrapper.write_text(
            "await import("
            + json.dumps((root / "node_modules/@opentui/solid/scripts/preload.js").as_uri())
            + ");\nprocess.chdir(process.env.CONVERGENCE_PROJECT!);\nawait import("
            + json.dumps((root / "src/index.ts").as_uri())
            + ");\n",
            encoding="utf-8",
        )
        env["CONVERGENCE_PROJECT"] = str(project)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(local_config(url, MODEL, 41, 0, output_tokens, 32768))
        env.update(
            {
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            }
        )
        for key in ("STATE", "DATA", "CONFIG", "CACHE"):
            env["XDG_" + key + "_HOME"] = str(directory / key.lower())
        return [
            r"C:/Users/Administrator/AppData/Roaming/npm/node_modules/bun/bin/bun.exe",
            str(wrapper),
            "run",
            "--standalone",
            "--format",
            "json",
            "--model",
            "kobold/" + MODEL,
            "--auto",
        ]
    root = ROOT / "reference-projects/deepseek-harness"
    env.update(
        {
            "DSH_HOME": str(directory / "dsh-home"),
            "DSH_TELEMETRY": "0",
            "CONVERGENCE_LOCAL_KEY": "local",
            "TSX_TSCONFIG_PATH": str(root / "tsconfig.json"),
        }
    )
    patch = directory / "dsh.patch.yml"
    config = [
        {"id": "agent-default-model", "config": {"provider": "kobold", "model": MODEL}},
        {
            "id": "llm-pi-ai",
            "config": {
                "providers": {
                    "kobold": {
                        "api": "openai-completions",
                        "apiKeyEnv": "CONVERGENCE_LOCAL_KEY",
                        "baseURL": url + "/v1",
                        "models": [
                            {"id": MODEL, "contextWindow": 32768, "maxTokens": output_tokens, "input": ["text"]}
                        ],
                        "compat": {
                            "maxTokensField": "max_tokens",
                            "supportsStore": False,
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                            "supportsStrictMode": False,
                            "chatTemplateKwargs": {"enable_thinking": False},
                        },
                        "retryPolicy": {"mode": "normal", "maxRetries": 0},
                    }
                }
            },
        },
    ]
    patch.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return [
        node,
        "--import",
        (root / "node_modules/tsx/dist/esm/index.mjs").as_uri(),
        str(root / "apps/cli/src/bin.ts"),
        "--profile",
        "headless",
        "--patch",
        str(patch),
        "--json",
        "--approval-policy",
        "never",
    ]


def source_identity(arm):
    root = ROOT / ("reference-projects/" + {"dsh": "deepseek-harness"}.get(arm, arm)) if arm != "codey" else ROOT
    manifest = (
        {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "codey").rglob("*"))
            if p.suffix in {".py", ".mjs", ".json"}
        }
        if arm == "codey"
        else None
    )
    return {
        "manifest": manifest,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "diff_sha256": hashlib.sha256(
            subprocess.check_output(
                ["git", "diff", "--binary", "--", "codey"] if arm == "codey" else ["git", "diff", "--binary"], cwd=root
            )
        ).hexdigest(),
    }


def scope_unchanged(project, case_name, case):
    actual = {
        p.relative_to(project).as_posix()
        for p in project.rglob("*")
        if p.is_file() and not {".git", "__pycache__"}.intersection(p.relative_to(project).parts)
    }
    if actual != set(case["files"]):
        return False
    editable = {"app.py", "adapter.py"} if case_name == "multi-file" else set()
    return all(
        (project / name).read_text(encoding="utf-8") == content
        for name, content in case["files"].items()
        if name not in editable
    )


def run(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    if "codey" in args.arms:
        with zipfile.ZipFile(out / "production-source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in sorted((ROOT / "codey").rglob("*")):
                if file.suffix in {".py", ".mjs", ".json"}:
                    archive.write(file, file.relative_to(ROOT).as_posix())
    projects = Path(tempfile.mkdtemp(prefix="codey-12b-comparison-")).resolve()
    report = {
        "project_root": str(projects),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": MODEL,
        "upstream": args.upstream,
        "cases": CASES,
        "rows": [],
        "constraints": {
            "context_window": 32768,
            "output_tokens": args.output_tokens,
            "request_limit": args.request_limit,
            "seconds": args.timeout,
            "seed": 41,
            "temperature": 0,
        },
        "sampling_note": "DSH uses its native configured route defaults; actual controls are in each HTTP journal.",
    }
    for case_index, case_name in enumerate(args.cases):
        case = CASES[case_name]
        shift = case_index % len(args.arms)
        arms = args.arms[shift:] + args.arms[:shift]
        for arm in arms:
            _wait_for_backend_idle(args.upstream, timeout=90)
            directory = out / case_name / arm
            directory.mkdir(parents=True)
            project = projects / case_name / arm
            project.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
            top = subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "--show-toplevel"], text=True
            ).strip()
            if Path(top).resolve() != project:
                raise RuntimeError("fixture is not an independent Git workspace")
            for name, text in case["files"].items():
                file = project / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text, encoding="utf-8")
            hooks = directory / "empty-hooks"
            hooks.mkdir()
            git = [
                "git",
                "-C",
                str(project),
                "-c",
                "core.autocrlf=false",
                "-c",
                f"core.hooksPath={hooks}",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Harness Fixture",
                "-c",
                "user.email=fixture@localhost",
            ]
            git_env = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            }
            baseline_commit = ""
            if not args.untracked_fixture:
                subprocess.run([*git, "add", "--", *case["files"]], env=git_env, check=True, capture_output=True)
                subprocess.run(
                    [*git, "commit", "--no-verify", "-qm", "fixture baseline"], env=git_env, check=True, capture_output=True
                )
                baseline_commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            trace = directory / "trace"
            trace.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN"))}
            env.update(
                {
                    "PYTHONPATH": str(ROOT),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "NATIVE_TOOLS": "1",
                    "AGENT_AB_TRACE": str(trace),
                    "AGENT_AB_SEED": "41",
                    "AGENT_AB_TEMPERATURE": "0",
                    "PI_OFFLINE": "1",
                    "PI_TELEMETRY": "0",
                    "LOCAL_OPENAI_MODEL": MODEL,
                    "LOCAL_OPENAI_API_KEY": "local",
                    "LOCAL_OPENAI_CONTEXT_WINDOW": "32768",
                    "LOCAL_OPENAI_CONTEXT_RESERVE": str(args.output_tokens),
                    "LOCAL_OPENAI_CONTEXT_KEEP": "12000",
                }
            )
            proxy = Proxy(
                ("127.0.0.1", 0),
                args.upstream,
                args.timeout,
                journal=directory / "requests.jsonl",
                request_limit=args.request_limit,
            )
            proxy.active_arm = arm + "/" + case_name
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{proxy.server_port}"
            env["LOCAL_OPENAI_BASE_URL"] = url + "/v1"
            phase_dir = directory / "phase"
            phase_dir.mkdir()
            row = {
                "arm": arm,
                "case": case_name,
                "project": str(project),
                "fixture_commit": baseline_commit,
                "source": source_identity(arm),
            }
            try:
                cmd = command(arm, directory, project, url, env, args.output_tokens)
                if arm == "codey" and case["expected"] is not None:
                    cmd += ["--output-format", "json"]
                cmd += [case["task"]]
                (directory / "command.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")
                phase = _run_process(cmd, project, env, phase_dir, deadline=time.monotonic() + args.timeout, control="")
                text = final_text(arm, phase["rows"])
                if case["expected"] is not None:
                    try:
                        content_ok = json.loads(text) == case["expected"]
                    except ValueError:
                        content_ok = False
                    strict_json_ok = content_ok
                    semantic_text = text.strip()
                    if semantic_text.startswith("```json\n") and semantic_text.endswith("\n```"):
                        semantic_text = semantic_text[8:-4]
                    try:
                        content_ok = json.loads(semantic_text) == case["expected"]
                    except ValueError:
                        content_ok = False
                    scope_ok = all(
                        (project / name).read_text(encoding="utf-8") == value for name, value in case["files"].items()
                    )
                    proof_ok = proxy.generation_requests >= 2
                else:
                    strict_json_ok = None
                    oracle_env = {**env, "AGENT_AB_ORACLE": "1"}
                    oracle = subprocess.run(
                        [sys.executable, "-m", "unittest", "discover", "-q"],
                        cwd=project,
                        env=oracle_env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=20,
                    )
                    (directory / "oracle.txt").write_text(oracle.stdout + oracle.stderr, encoding="utf-8")
                    content_ok = oracle.returncode == 0
                    scope_ok = (project / "test_app.py").read_text(encoding="utf-8") == case["files"]["test_app.py"]
                    proof_ok = any(
                        r.get("kind") == "verification_finished" and r.get("passed") for r in execution_rows(trace)
                    )
                scope_ok = scope_unchanged(project, case_name, case)
                row.update(
                    {
                        "status": phase["status"],
                        "returncode": phase["returncode"],
                        "seconds": phase["wall_time_seconds"],
                        "answer": text,
                        "strict_json_ok": strict_json_ok,
                        "content_ok": content_ok,
                        "scope_ok": scope_ok,
                        "tool_or_verification_observed": proof_ok,
                        "success": content_ok and scope_ok and proof_ok and phase["returncode"] == 0,
                        "strict_success": content_ok
                        and scope_ok
                        and proof_ok
                        and phase["returncode"] == 0
                        and strict_json_ok is not False,
                        "forced_cleanup": phase["forced_cleanup"],
                        "requests": proxy.generation_requests,
                        "physical_requests": sum(
                            r.get("path") == "/v1/chat/completions" and not r.get("synthetic") for r in proxy.records
                        ),
                        "usage": usage_totals(proxy.records),
                    }
                )
            except Exception as exc:
                row.update({"status": "observer_error", "error": repr(exc), "success": False})
            finally:
                proxy.shutdown()
                proxy.server_close()
            report["rows"].append(row)
            (directory / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in row.items() if k != "source"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument(
        "--arms", nargs="+", choices=("codey", "pi", "opencode", "dsh"), default=("codey", "pi", "opencode", "dsh")
    )
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=tuple(CASES))
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output-tokens", type=int, default=2048)
    parser.add_argument("--request-limit", type=int, default=24)
    parser.add_argument("--untracked-fixture", action="store_true")
    raise SystemExit(run(parser.parse_args()))
