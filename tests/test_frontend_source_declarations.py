import json
import subprocess
from pathlib import Path


def test_runtime_source_reader_skips_type_only_declarations(tmp_path):
    (tmp_path / "only.d.ts").write_text("declare module 'only';", encoding="utf8")
    (tmp_path / "live.ts").write_text("export const count: number = 1;", encoding="utf8")
    result = subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "tools/frontend_source.mjs"), str(tmp_path)],
        check=True, capture_output=True, text=True, encoding="utf8")
    assert list(json.loads(result.stdout)) == ["live.ts"]
