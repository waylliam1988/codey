"""Isolated native attachment appearance review; never invokes a model.

Run with --artifacts <new-folder>. The initial window restores a real uploaded
fixture image. Write `supported` to advance.txt to change the controlled run's
image capability, or `quit` to close. Production HTTP, storage, SSE and frontend
render the states. Only the initial local draft reference is seeded in HTML.
"""
from __future__ import annotations

import argparse
import json
import threading
from io import BytesIO
from pathlib import Path
from unittest import mock

from codey.app import attachment_routes, server
from codey.app.service import LocalService
from codey.runtime.core.api_selection import ApiRunSelection

CHAT = "attachment-native-review"
INITIAL = "attachment-native-text"
SUPPORTED = "attachment-native-vision"
IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    root = args.artifacts.resolve()
    root.mkdir(parents=True, exist_ok=False)
    service = LocalService(root / "state", verify_only=True, release_id="source:attachment-review").start()
    ctx = service.ctx
    ctx.save_ui_state({"active_id": CHAT, "projects": [], "sessions": [
        {"id": CHAT, "title": "Attachment review", "provider": "deepseek"}]}, base_revision=0)
    status, uploaded = attachment_routes.upload_response(ctx, {"session_id": CHAT, "name": "screen.png", "data": IMAGE})
    assert status == 201, uploaded
    reference = uploaded["attachment"]

    def run(identity: str, supports_images: bool) -> None:
        ctx.durable.admit_input(conversation_id=CHAT, request_id=identity, task_id=identity,
                                text="Inspect the attached screenshot.", provider_snapshot={"provider_id": "local"})
        ctx.durable.start_task(identity, state={})
        ctx.reserve_run(session_id=CHAT, project=None, task="Attachment appearance review", provider_id="local", run_id=identity)
        if supports_images:
            ctx.run_registry.bind_api_selection(identity, ApiRunSelection("local", "fixture", "vision-fixture", "openai-completions", True, supports_images=True))

    run(INITIAL, False)
    original = server.send_index

    def index(handler) -> None:
        output = mock.Mock(headers=handler.headers, wfile=BytesIO())
        original(output)
        draft = json.dumps([[CHAT, [reference]]], ensure_ascii=True)
        init = ("<script>localStorage.setItem('codey:attachment-drafts:v1'," + json.dumps(draft) + ");</script>").encode()
        body = output.wfile.getvalue().replace(b"<head>", b"<head>" + init, 1)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    server.send_index = index
    import webview

    window = webview.create_window("Codey | Attachment review", url=service.httpd.launch_url(f"http://127.0.0.1:{service.httpd.server_port}/"),
                                   width=1380, height=950, text_select=True, focus=False)
    assert window is not None
    closed = threading.Event()
    window.events.closed += closed.set

    def control() -> None:
        (root / "ready.txt").write_text("Native attachment fixture ready.", encoding="utf-8")
        advanced = False
        for _ in range(1200):
            if closed.wait(0.2):
                return
            path = root / "advance.txt"
            value = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            if value == "quit":
                window.destroy()
                return
            if value == "supported" and not advanced:
                advanced = True
                ctx.durable.complete_task(INITIAL, "Fixture transition", state={})
                ctx.release_run(INITIAL)
                run(SUPPORTED, True)
                ctx.emit({"type": "task_done", "session_id": CHAT, "run_id": INITIAL,
                          "stop_reason": "done", "summary": "Fixture transition", "busy": True})
                (root / "stage.txt").write_text("supported", encoding="utf-8")
        window.destroy()

    try:
        webview.start(func=control, private_mode=True, storage_path=str(root / "webview"))
    finally:
        closed.set()
        server.send_index = original
        ctx.release_run(INITIAL)
        ctx.release_run(SUPPORTED)
        service.close()


if __name__ == "__main__":
    main()
