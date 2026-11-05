"""Fresh-process absence test: remove only Cline's registration and imports."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.support.optional_zen_removal_probe import probe


def child_headless(root):
    helper = root / "child-startup"
    helper.mkdir()
    attempted = root / "child-import-attempts.txt"
    # Every actual child (including gate workers) installs the same absence
    # boundary before importing application modules. No production overrides.
    startup = '''import importlib.abc, sys
from pathlib import Path
class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "codey.providers.cline" or fullname.startswith("codey.providers.cline."):
            Path(ATTEMPTS).write_text(fullname)
            raise ModuleNotFoundError("optional package absent")
sys.meta_path.insert(0, Missing())
from codey.providers.catalog import API_CONNECTIONS, PROVIDER_LABELS
API_CONNECTIONS.pop("cline", None)
PROVIDER_LABELS.pop("cline", None)
'''.replace("ATTEMPTS", repr(str(attempted)))
    (helper / "sitecustomize.py").write_text(startup, encoding="utf8")

    reviewing = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            content = json.dumps({"verdict": "approved", "summary": "Looks good", "findings": []}) if reviewing else "KOBOLD_OK"
            raw = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    from tools.local_model_gate_attempts import GateTarget, run_case_process

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    repo = Path(__file__).resolve().parents[2]
    pythonpath = os.pathsep.join((str(helper), str(repo), os.environ.get("PYTHONPATH", "")))
    try:
        with patch.dict(os.environ, {"PYTHONPATH": pythonpath}):
            result = subprocess.run([sys.executable, "-c", "from codey.app.cli import main; main(['--help'])"],
                cwd=repo, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            target = GateTarget(f"http://127.0.0.1:{server.server_port}/v1", "fixture", 32768, 8192, 12000)
            result = run_case_process("chat", root / "headless-gate", target, timeout=20)
            assert result["ok"], result
        reviewing = True
        review_without_cline(root, target.base_url)
        assert not attempted.exists(), attempted.read_text() if attempted.exists() else ""
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def review_without_cline(root, base):
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.providers import local_config
    from tests.support.model_preferences import enable_models

    state_home = root / "review-state"
    project = root / "review-project"
    project.mkdir()
    source = project / "app.py"
    source.write_text("x = 2\n", encoding="utf8")
    rows = []
    with patch.object(local_config, "DEFAULT_STATE_HOME", state_home):
        local_config.save_local_config(local_config.LocalProviderConfig(
            base_url=base, model="fixture", native_tools_mode="on", connection_revision="fixture-review"))
        enable_models(state_home, local=["fixture"])
        result = run_headless(HeadlessRequest(project=project, task="Review changes", intent="review",
            provider_id="local", model_selection={"model": "fixture", "base_url": base}, state_home=state_home),
            collect_changes=lambda *_: {"ok": True, "files": [{"path": "app.py"}], "changed_count": 1,
                "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"},
            emit_jsonl=rows.append)
    assert result.exit_code == 0, rows
    assert next(row for row in rows if row["type"] == "task_done")["review"]["status"] == "complete"
    assert source.read_text(encoding="utf8") == "x = 2\n"


def main(root):
    report = probe(root, removed="cline")
    # The remaining optional connector keeps its actual production envelope.
    # A blocked import finder remains installed during these requests too.
    from codey.providers import api_generation
    from codey.providers.api_context import fork_auxiliary
    from codey.providers.api_provider import ApiProvider
    from codey.providers.zen.connection import ZenProvider

    seen = []

    def generate(endpoint, payload, *args, **kwargs):
        seen.append(payload)
        assert {"read", "shell"} <= {tool["function"]["name"] for tool in payload["tools"]}
        return {"choices": [{"message": {"content": "Zen still works"}, "finish_reason": "stop"}]}

    runtime = ApiProvider("http://fixture/v1", "fixture", native_tools=True)
    provider = ZenProvider(runtime)
    runtime.auxiliary_connection = ZenProvider
    with patch.object(api_generation, "generate", generate):
        assert provider.send("hello") == "Zen still works"
        child = fork_auxiliary(runtime)
        try:
            assert ZenProvider(child).send("summary") == "Zen still works"
        finally:
            child.close()
    provider.close()
    assert len(seen) == 2
    assert not any(name.startswith("codey.providers.cline") for name in sys.modules)
    report.update(zen_without_cline=True, auxiliary_without_cline=True)
    child_headless(root)
    report.update(child_cli_without_cline=True, headless_without_cline=True, review_without_cline=True)
    return report


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]))))
