"""Architecture and publish contracts for the TypeScript frontend."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
WEB = ROOT / "codey" / "web"

def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")

def _source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (FRONTEND / "src").rglob("*.ts"))

def test_required_runtime_modules_are_imported() -> None:
    main = (FRONTEND / "src/main.ts").read_text(encoding="utf-8")
    runtime = (FRONTEND / "src/app_runtime.ts").read_text(encoding="utf-8")
    assert main.strip() == "import './app_runtime';"
    for name in ("operator_auth", "provider_ui", "ui_state", "sse", "composer", "conversation_ui"):
        assert f"./{name}" in runtime

def test_every_html_resource_reference_exists_in_public_or_publish_output() -> None:
    refs = re.findall(r'(?:src|href)="([^"]+)"', _html())
    for ref in refs:
        path = ref.split("?", 1)[0].lstrip("/")
        assert path.startswith(("assets/", "icon.ico", "src/"))
        if path.startswith("src/"):
            assert (FRONTEND / path).is_file()
        else:
            assert (WEB / path).is_file() or (FRONTEND / "public" / path).is_file()

def test_resource_extensions_are_served_types() -> None:
    from codey.app.http_plumbing import WEB_ASSET_TYPES

    for path in (WEB / "assets").iterdir():
        assert path.suffix in WEB_ASSET_TYPES, path

def test_frontend_has_no_inline_css() -> None:
    assert not re.search(r"<style\b", _html(), re.I)

def test_business_script_is_not_inline_in_html() -> None:
    class ScriptParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.scripts: list[tuple[dict[str, str], str]] = []
            self._attrs: dict[str, str] | None = None
            self._body: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "script":
                self._attrs = {key: value or "" for key, value in attrs}
                self._body = []

        def handle_data(self, data: str) -> None:
            if self._attrs is not None:
                self._body.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag == "script" and self._attrs is not None:
                self.scripts.append((self._attrs, "".join(self._body)))
                self._attrs = None

    parser = ScriptParser()
    parser.feed(_html())
    assert len(parser.scripts) == 1
    attrs, body = parser.scripts[0]
    assert attrs == {"type": "module", "src": "/src/main.ts"}
    assert not body.strip()


def test_production_runtime_does_not_publish_module_globals() -> None:
    names = (
        "Render", "UiState", "ProviderUI", "Composer", "ConversationUI", "Messages",
        "Sse", "Requests", "Process", "ChangesDrawer", "LocalContextDrawer",
        "ResearchDrawer", "ResearchRuns", "RunDetails", "OperatorAuth", "Settings",
        "ModelSettings", "ResearchGraph", "ConversationNav", "Models",
    )
    source_files = [
        path for path in (FRONTEND / "src").glob("*.ts")
        if not path.name.endswith(".test.ts") and path.name != "runtime.ts"
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    for name in names:
        assert f"window.{name}" not in source
    bundle = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "assets").glob("*.js"))
    for name in names:
        assert f"window.{name}" not in bundle


def test_publish_manifest_is_present_and_matches_build_schema() -> None:
    data = json.loads((WEB / "frontend-build.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["files"]

def test_publish_output_contains_compiled_javascript() -> None:
    assert list((WEB / "assets").glob("*.js"))

def test_publish_output_contains_css() -> None:
    assert (WEB / "assets/app.css").is_file()
    assert (WEB / "assets/tokens.css").is_file()

def test_source_and_public_are_separate_from_node_artifacts() -> None:
    assert not (WEB / "node_modules").exists()
    assert not (WEB / "coverage").exists()

def test_production_bundle_has_no_test_markers() -> None:
    bundle = "\n".join(p.read_text(encoding="utf-8") for p in (WEB / "assets").glob("*.js"))
    assert "vitest" not in bundle.lower()
    assert "@testing-library" not in bundle.lower()

def test_public_assets_are_copied_into_publish_output() -> None:
    for path in (FRONTEND / "public").rglob("*"):
        if path.is_file():
            assert (WEB / path.relative_to(FRONTEND / "public")).is_file()
