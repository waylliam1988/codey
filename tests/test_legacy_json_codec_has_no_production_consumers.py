"""The legacy JSON codec remains outside the production protocol boundary."""

from __future__ import annotations

from pathlib import Path


def test_legacy_json_codec_has_no_codey_or_tools_consumers() -> None:
    root = Path(__file__).resolve().parents[1]
    production_roots = (root / "codey", root / "tools")
    forbidden = ("JsonToolCodec(", "from tests.support.legacy_json_codec import", "import tests.support.legacy_json_codec")
    hits: list[str] = []
    for base in production_roots:
        for path in base.rglob("*.py"):
            if path.name == "json_codec.py":
                continue
            text = path.read_text(encoding="utf-8")
            if any(token in text for token in forbidden):
                hits.append(path.relative_to(root).as_posix())
    assert hits == []
