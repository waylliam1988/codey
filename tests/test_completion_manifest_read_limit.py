from os import stat_result
from pathlib import Path

import pytest

from codey.completion.discovery import read_manifest_text


def test_manifest_read_checks_the_size_of_the_open_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_bytes(b"{}\r\n")
    assert read_manifest_text(manifest, max_bytes=4) == "{}\n"

    saved_stat = manifest.stat()
    manifest.write_bytes(b'{"scripts":{"test":"vitest"}}\n')
    stat = Path.stat

    def stale_stat(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        if path == manifest:
            return saved_stat
        return stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stale_stat)
    assert read_manifest_text(manifest, max_bytes=4) == ""
