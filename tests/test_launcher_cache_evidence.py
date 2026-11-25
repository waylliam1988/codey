"""Invalid optional evidence fields cannot validate a release."""

from pathlib import Path

from codey.launcher.validation_cache import cached_checks
from codey.storage.local_store import write_json_atomic


def test_cache_rejects_non_numeric_check_duration(tmp_path: Path) -> None:
    path = tmp_path / "validation-cache" / "cache.json"
    commands = [["python", "--help"]]
    verification: dict[str, object] = {
        "argv": commands[0], "state": "settled", "exit_code": 0,
        "started_at": 1.0, "finished_at": 2.0, "elapsed_seconds": "fast",
    }
    write_json_atomic(path, {"key": "cache", "commands": commands, "verification": [verification]})

    assert cached_checks(tmp_path, "cache", commands) is None

    verification["elapsed_seconds"] = 1.0
    write_json_atomic(path, {"key": "cache", "commands": commands, "verification": [verification]})
    assert cached_checks(tmp_path, "cache", commands) == [{**verification, "state": "cached", "cache_key": "cache"}]
