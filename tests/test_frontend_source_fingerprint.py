import pytest

from tools import publish_frontend as publisher


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "ROOT", tmp_path)
    for name in publisher.BUILD_INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("build input", encoding="utf-8")
    for name in ("frontend/src/app.ts", "frontend/public/assets/tokens.css"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("production", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("name", ["flow.test.ts", "nested/flow.spec.tsx", "flow.test.js", "flow.spec.jsx"])
def test_only_explicit_test_files_are_excluded_from_production_fingerprint(source, name):
    before = publisher._source_digest()
    path = source / "frontend/src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test one", encoding="utf-8")
    assert publisher._source_digest() == before
    path.write_text("test two", encoding="utf-8")
    assert publisher._source_digest() == before
    path.unlink()
    assert publisher._source_digest() == before


@pytest.mark.parametrize("name", [*publisher.BUILD_INPUTS, "frontend/src/app.ts",
    "frontend/public/assets/tokens.css", "frontend/src/test_support.ts", "frontend/src/specification.ts"])
def test_production_assets_dependencies_configs_and_publisher_still_invalidate(source, name):
    before = publisher._source_digest()
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed", encoding="utf-8")
    assert publisher._source_digest() != before
