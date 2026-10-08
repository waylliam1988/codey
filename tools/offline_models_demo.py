"""Inspect the shipped model manager on loopback with in-memory fixtures only.

Run: python -m tools.offline_models_demo --port 8767
No Codey/provider imports, credentials, outbound requests, or inference.
"""

from __future__ import annotations

import argparse
import copy
import json
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

from tools.offline_retry_demo import Demo, Handler


class ModelsDemo(Demo):
    def reset(self):
        super().reset()
        self.sources = [
            {
                "id": "websites",
                "label": "Websites",
                "models": [
                    {"id": key, "name": name}
                    for key, name in [
                        ("deepseek", "DeepSeek"),
                        ("mimo", "MiMo"),
                        ("stepfun", "StepFun"),
                        ("qwen", "Qwen"),
                        ("glm", "GLM"),
                    ]
                ],
            },
            {
                "id": "zen",
                "label": "OpenCode Zen",
                "discoverable": True,
                "models": [
                    {"id": key, "name": name, "efforts": ["off", "low", "medium", "high"]}
                    for key, name in [
                        ("zen-fixture-a", "Zen model A"),
                        ("zen-fixture-b", "Zen model B"),
                        ("zen-fixture-c", "Zen model C"),
                    ]
                ],
            },
            {
                "id": "local",
                "label": "Local",
                "connection_editor": True,
                "discoverable": True,
                "base_url": "http://127.0.0.1:59999/v1",
                "default_model": "local-fixture-a",
                "models": [
                    {"id": "local-fixture-a", "name": "Gemma4 12B", "efforts": ["off", "low", "high"]},
                    {"id": "local-fixture-b", "name": "Local model B", "efforts": []},
                ],
            },
        ]
        self.preferences = {
            "revision": 0,
            "sources": {
                "websites": {"enabled": True, "models": ["deepseek", "qwen"]},
                "zen": {"enabled": True, "models": ["zen-fixture-a"]},
                "local": {"enabled": True, "models": ["local-fixture-a"]},
            },
        }
        self.saved = {
            "active_id": "models-demo",
            "revision": 0,
            "projects": [],
            "sessions": [
                {"id": "models-demo", "title": "Model management · Offline", "provider": "deepseek", "messages": []},
                {
                    "id": "local-demo",
                    "title": "Local model · Offline",
                    "provider": "local",
                    "messages": [],
                    "modelSelection": {
                        "connection_id": "local",
                        "model": "local-fixture-a",
                        "name": "Gemma4 12B",
                        "base_url": "http://127.0.0.1:59999/v1",
                        "effort": "low",
                        "efforts": {},
                    },
                },
            ],
        }

    def settings(self):
        used = (
            [{"provider": self.state["provider"], "model": self.state.get("model", "")}] if self.state["busy"] else []
        )
        return {"ok": True, "sources": self.sources, "preferences": self.preferences, "in_use": used}

    def allowed(self, provider, model=""):
        source = "websites" if provider in {m["id"] for m in self.sources[0]["models"]} else provider
        choice = self.preferences["sources"].get(source, {})
        return choice.get("enabled") and (provider if source == "websites" else model) in choice.get("models", [])

    def run(self, body):
        model = body.get("model_selection", {}).get("model", "")
        if not self.allowed(body["provider"], model):
            return 409, {"error": "Selected model is disabled.", "code": "model_disabled"}
        self.attempts[body["session_id"]] = 2
        result = super().run(body)
        if result[0] == 200:
            self.state.update(provider=body["provider"], model=model)
        return result

    def save_models(self, body):
        if body.get("base_revision") != self.preferences["revision"]:
            return 409, {"error": "Model settings changed; reload Settings."}
        choices = body["sources"]
        old = self.preferences
        self.preferences = {"revision": old["revision"] + 1, "sources": copy.deepcopy(choices)}
        if self.state["busy"] and not self.allowed(self.state["provider"], self.state.get("model", "")):
            self.preferences = old
            return 409, {"error": "This model is in use. Stop the task before disabling it.", "code": "model_in_use"}
        return 200, self.settings()


class ModelsHandler(Handler):
    def do_GET(self):
        path = urlsplit(self.path).path
        demo = self.server.demo
        with demo.lock:
            if path == "/api/model_settings":
                self.respond(demo.settings())
            elif path in {"/api/provider_catalog", "/api/providers"}:
                self.respond(
                    {
                        "default": "deepseek",
                        "providers": [
                            {"id": model["id"], "label": model["name"], "available": demo.allowed(model["id"])}
                            for model in demo.sources[0]["models"]
                        ]
                        + [
                            {
                                "id": source["id"],
                                "label": source["label"],
                                "available": demo.preferences["sources"][source["id"]]["enabled"],
                            }
                            for source in demo.sources[1:]
                        ],
                    }
                )
            elif path == "/api/api_models":
                self.respond(
                    {
                        "connections": [
                            source
                            for source in demo.sources[1:]
                            if demo.preferences["sources"][source["id"]]["enabled"]
                        ]
                    }
                )
            elif path == "/api/local_provider":
                local = demo.sources[-1]
                self.respond(
                    {
                        "ok": True,
                        "local": {
                            "connected": True,
                            "base_url": local["base_url"],
                            "model": local["default_model"],
                            "models": [model["id"] for model in local["models"]],
                            "display_name": local["models"][0]["name"],
                            "context": {"context_window_tokens": 32768},
                            "native_tools_mode": "auto",
                            "has_api_key": False,
                        },
                    }
                )
            else:
                # SSE is a long-lived request; never hold the demo lock through it.
                pass
        if path not in {
            "/api/model_settings",
            "/api/provider_catalog",
            "/api/providers",
            "/api/api_models",
            "/api/local_provider",
        }:
            super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/api/model_settings", "/api/model_catalog", "/api/local_provider"}:
            return super().do_POST()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        demo = self.server.demo
        with demo.lock:
            if path == "/api/model_settings":
                status, data = demo.save_models(body)
                self.respond(data, status)
            elif path == "/api/model_catalog":
                source = next(source for source in demo.sources if source["id"] == body["source"])
                new_id = source["id"] + "-new-arrival"
                if not any(model["id"] == new_id for model in source["models"]):
                    source["models"].append({"id": new_id, "name": "New arrival", "efforts": []})
                self.respond({"ok": True, "source": source})
            else:
                local = demo.sources[-1]
                local.update(base_url=body["base_url"], default_model=body["model"])
                local["models"] = [
                    {"id": body["model"], "name": body.get("display_name") or body["model"], "efforts": ["off", "high"]}
                ]
                self.respond(
                    {
                        "ok": True,
                        "local": {
                            "connected": True,
                            "base_url": local["base_url"],
                            "model": body["model"],
                            "models": [body["model"]],
                        },
                    }
                )


def serve(port):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ModelsHandler)
    httpd.daemon_threads = True
    httpd.demo = ModelsDemo()
    print(f"Offline model inspection: http://127.0.0.1:{httpd.server_port}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    serve(parser.parse_args().port)
