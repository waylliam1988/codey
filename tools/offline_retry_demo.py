"""Serve the shipped UI with a loopback-only, credential-free retry simulator.

Run: python tools/offline_retry_demo.py --port 8766
The seeded 503 fails once more with 403, then succeeds. All API routes are
simulated in memory; this module does not import Codey or any provider client.
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

WEB = Path(__file__).resolve().parents[1] / "codey" / "web"
FIRST_RUN = "run_" + "0" * 32
ERROR_503 = "model HTTP 503 [server_error]: The backend is temporarily overloaded. Please retry."
ERROR_403 = "model HTTP 403: OpenCode's free tier can only be used from within OpenCode."


class Demo:
    def __init__(self):
        self.lock = threading.RLock()
        self.subscribers: set[queue.Queue] = set()
        self.cursor = 0
        self.reset()

    def reset(self):
        with self.lock:
            self.generation = getattr(self, "generation", 0) + 1
            self.state = {"busy": False}
            self.saved = {"active_id": "retry-demo", "revision": 0, "updated_at": 0, "projects": [], "sessions": [
                {"id": "retry-demo", "title": "Retry inspection · Offline", "provider": "deepseek", "terminalRuns": [FIRST_RUN],
                 "messages": [{"type": "user", "id": "demo-request", "text": "你好", "attempts": [
                     {"runId": FIRST_RUN, "state": "failed", "error": ERROR_503, "warning": "Local update paused"}]},
                     {"type": "request_status", "sessionId": "retry-demo", "requestId": "demo-request"}]},
                {"id": "draft-demo", "title": "Draft inspection · Offline", "provider": "mimo", "messages": []}]}
            self.attempts = {"retry-demo": 1}

    def publish(self, event):
        with self.lock:
            self.cursor += 1
            event = {**event, "event_id": self.cursor}
            for subscriber in self.subscribers:
                subscriber.put(event)

    def run(self, body):
        with self.lock:
            if self.state["busy"]:
                return 409, {"error": "busy"}
            run_id, sid = body["run_id"], body["session_id"]
            self.attempts[sid] = self.attempts.get(sid, 0) + 1
            number = self.attempts[sid]
            generation = self.generation
            self.state = {"busy": True, "run_id": run_id, "session_id": sid, "run_status": "running"}

        def complete():
            with self.lock:
                if generation != self.generation:
                    return
                summary = "ERROR: " + (ERROR_503 if number == 1 else ERROR_403) if number < 3 else "你好！这是本地模拟回复。重试已成功，提问始终只保留一条。"
                event = {"type": "task_done", "run_id": run_id, "session_id": sid, "mode": "chat",
                         "stop_reason": "error" if number < 3 else "done", "summary": summary}
                self.state = {"busy": False, "last_terminal_event": event}
                if number < 3:
                    self.publish({"type": "ghost_post_turn_warning", "run_id": run_id, "session_id": sid})
                self.publish(event)

        def start():
            with self.lock:
                if generation == self.generation:
                    self.publish({"type": "task_start", "run_id": run_id, "session_id": sid})

        threading.Timer(0.15, start).start()
        threading.Timer(2, complete).start()
        return 200, {"ok": True, "run_id": run_id}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond(self, data, status=200, content_type="application/json"):
        payload = json.dumps(data, ensure_ascii=False).encode() if content_type == "application/json" else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-src 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        demo = self.server.demo
        if path == "/api/events":
            subscriber = queue.Queue()
            with demo.lock:
                demo.subscribers.add(subscriber)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"type":"hello"}\n\n')
                self.wfile.flush()
                while True:
                    try:
                        event = subscriber.get(timeout=3)
                        payload = f'id: {event["event_id"]}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'.encode()
                    except queue.Empty:
                        payload = b": heartbeat\n\n"
                    self.wfile.write(payload)
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass
            finally:
                with demo.lock:
                    demo.subscribers.discard(subscriber)
            return
        if path.startswith("/api/"):
            with demo.lock:
                if path == "/api/ui_state":
                    data = {"ok": True, "state": demo.saved}
                elif path == "/api/state":
                    data = demo.state
                elif path in {"/api/provider_catalog", "/api/providers"}:
                    data = {"default": "deepseek", "providers": [
                        {"id": "deepseek", "label": "Offline model A", "available": True},
                        {"id": "mimo", "label": "Offline model B", "available": True}]}
                elif path == "/api/api_models":
                    data = {"ok": True, "connections": []}
                elif path == "/api/model_settings":
                    data = {"ok": True, "preferences": {"revision": 0, "sources": {
                        "websites": {"enabled": True, "models": ["deepseek", "mimo"]}}},
                        "sources": [{"id": "websites", "label": "Websites", "models": [
                            {"id": "deepseek", "name": "Offline model A"}, {"id": "mimo", "name": "Offline model B"}]}], "in_use": []}
                elif path == "/api/local_provider":
                    data = {"ok": True, "local": {"connected": False}}
                else:
                    data = {"ok": True}
                self.respond(data)
            return
        if path == "/":
            data = (WEB / "index.html").read_text(encoding="utf-8").replace("__APP_VERSION__", "offline-retry")
            self.respond(data.encode(), content_type="text/html; charset=utf-8")
        elif path.startswith("/assets/") and Path(path).name == path.removeprefix("/assets/"):
            asset = WEB / "assets" / Path(path).name
            if asset.is_file() and asset.suffix in {".js", ".css"}:
                self.respond(asset.read_bytes(), content_type="application/javascript" if asset.suffix == ".js" else "text/css")
            else:
                self.respond({}, 404)
        else:
            self.respond({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        path = urlsplit(self.path).path
        demo = self.server.demo
        with demo.lock:
            if path == "/api/run":
                status, data = demo.run(body)
            elif path == "/api/ui_state":
                demo.saved = body["state"]
                data, status = {"ok": True, "revision": demo.saved.get("revision", 0)}, 200
            elif path == "/api/demo/reset":
                demo.reset()
                data, status = {"ok": True}, 200
            elif path == "/api/stop":
                active = demo.state
                event = {"type": "task_done", "run_id": active.get("run_id"), "session_id": active.get("session_id"), "stop_reason": "stopped"}
                demo.generation += 1
                demo.state = {"busy": False, "last_terminal_event": event}
                demo.publish(event)
                data, status = {"ok": True}, 200
            else:
                data, status = {"ok": True}, 200
            self.respond(data, status)


def serve(port):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.demo = Demo()
    print(f"Offline retry inspection: http://127.0.0.1:{httpd.server_port}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    serve(parser.parse_args().port)
