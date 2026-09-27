"""Cold-start v1 locks (red-first).

Desired contracts after the cleanup:
- Runtime operation state, local config, Ghost control surface are schema v1.
- Local config read requires type(schema_version) is int and == 1.
- Provider profiles require exact v1 (file schema + each profile).
- Shell approval event fields require the current complete record and verify
  truncation + hash; legacy-only-command records are rejected.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import mock


def _op_state(leaf="accepted"):
    from codey.runtime.core.operation_state import RuntimeOperationState

    return RuntimeOperationState(
        session_id="s1",
        run_id="run-1",
        operation_id="task:" + "1" * 24,
        lane="run:" + "2" * 24,
        project_ref="project:" + "3" * 24,
        provider_id="deepseek",
        turn_budget=5,
        max_repair_rounds=1,
        leaf=leaf,
        started_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
        task_kind="project",
    )


def test_operation_schema_is_v1() -> None:
    from codey.runtime.core import operation_state as mod

    assert mod.SCHEMA_VERSION == 1
    assert mod.KIND == "runtime_operation_state"
    payload = _op_state().to_payload()
    assert payload["schema_version"] == 1


def test_operation_rejects_bool_schema_version() -> None:
    from codey.runtime.core.operation_state import RuntimeOperationState

    payload = _op_state().to_payload()
    bool_payload = dict(payload)
    bool_payload["schema_version"] = True
    assert RuntimeOperationState.from_payload(bool_payload) is None
    # float 1.0 must also be rejected even though 1.0 == 1
    float_payload = dict(payload)
    float_payload["schema_version"] = 1.0
    assert RuntimeOperationState.from_payload(float_payload) is None


def test_operation_rejects_v2_payload_after_coldstart() -> None:
    from codey.runtime.core.operation_state import RuntimeOperationState

    payload = _op_state().to_payload()
    # Simulate a dev-period v2 log: must fail closed under v1.
    v2_payload = dict(payload)
    v2_payload["schema_version"] = 2
    assert RuntimeOperationState.from_payload(v2_payload) is None


def test_local_config_schema_is_v1() -> None:
    from codey.providers import local_config as mod

    assert mod.SCHEMA_VERSION == 1


def test_config_from_dict_requires_v1() -> None:
    import pytest

    from codey.providers import local_config as mod

    base = {
        "schema_version": 1,
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen",
        "api_key": "",
        "native_tools_mode": "auto",
    }
    # Valid v1 passes.
    ok = mod.config_from_dict(dict(base))
    assert ok.base_url == "http://127.0.0.1:11434/v1"

    # Missing / wrong / non-int versions are explicit errors, never silent parse.
    for bad_version in (None, 0, 2, 999, "1", 1.0, True, False):
        bad = dict(base)
        if bad_version is None:
            bad.pop("schema_version", None)
        else:
            bad["schema_version"] = bad_version
        with pytest.raises((ValueError, TypeError)):
            mod.config_from_dict(bad)

    # Non-dict input is also an explicit error.
    with pytest.raises((ValueError, TypeError)):
        mod.config_from_dict(None)  # type: ignore[arg-type]
    with pytest.raises((ValueError, TypeError)):
        mod.config_from_dict([])  # type: ignore[arg-type]


def test_load_local_config_missing_returns_default_without_backup(tmp_path: Path) -> None:
    from codey.providers import local_config as mod

    path = tmp_path / "local-openai.json"
    assert not path.exists()
    with mock.patch.object(mod, "_config_path", return_value=path):
        loaded = mod.load_local_config()
    assert loaded.base_url == ""
    assert loaded.model == ""
    assert not list(tmp_path.glob("*.corrupt*"))


def test_load_local_config_rejects_unversioned_and_bad_version(tmp_path: Path) -> None:
    from codey.providers import local_config as mod

    # No version: must not be parsed as current config.
    path = tmp_path / "local-openai.json"
    path.write_text(
        json.dumps({"base_url": "http://127.0.0.1:5001/v1", "model": "g"}),
        encoding="utf-8",
    )
    with mock.patch.object(mod, "_config_path", return_value=path):
        loaded = mod.load_local_config()
    assert loaded.base_url == ""  # fail closed to default, never the stale address
    assert loaded.model == ""
    # Corrupt/version-bad file is backed up for forensics.
    assert not path.exists() or path.read_text(encoding="utf-8") != json.dumps(
        {"base_url": "http://127.0.0.1:5001/v1", "model": "g"}
    )
    backups = list(tmp_path.glob("local-openai.json.corrupt*"))
    assert backups, "expected a .corrupt backup for unversioned config"

    # Wrong version 2: same fail-closed + backup behavior.
    path2 = tmp_path / "local2.json"
    path2.write_text(
        json.dumps({"schema_version": 2, "base_url": "http://127.0.0.1:5001/v1"}),
        encoding="utf-8",
    )
    with mock.patch.object(mod, "_config_path", return_value=path2):
        loaded2 = mod.load_local_config()
    assert loaded2.base_url == ""
    assert list(tmp_path.glob("local2.json.corrupt*"))

    # Bool True must not pass as 1.
    path3 = tmp_path / "local3.json"
    path3.write_text(
        json.dumps({"schema_version": True, "base_url": "http://127.0.0.1:5001/v1"}),
        encoding="utf-8",
    )
    with mock.patch.object(mod, "_config_path", return_value=path3):
        loaded3 = mod.load_local_config()
    assert loaded3.base_url == ""
    assert list(tmp_path.glob("local3.json.corrupt*"))


def test_control_surface_schema_is_v1() -> None:
    from codey.ghost import control_surface as mod

    assert mod.CONTROL_SURFACE_SCHEMA_VERSION == 1


def test_bundled_profiles_are_exact_v1() -> None:
    import json as _json

    from codey.providers import profiles as mod

    raw = _json.loads(mod.PROFILE_PATH.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    for provider_id, payload in raw["profiles"].items():
        assert type(payload["version"]) is int, provider_id
        assert payload["version"] == 1, provider_id


def test_profile_parser_requires_exact_v1() -> None:
    import pytest

    from codey.providers.profiles import _parse_profile, load_profiles

    good = {
        "version": 1,
        "hosts": ["example.com"],
        "selectors": {
            "message_box": ["textarea"],
            "send_button": ["button"],
            "response": [".r"],
        },
    }
    parsed = _parse_profile("demo", dict(good))
    assert parsed.version == 1

    for bad_version in (0, 2, 999, "1", 1.0, True, False, None):
        bad = dict(good)
        bad["version"] = bad_version
        with pytest.raises(RuntimeError):
            _parse_profile("demo", bad)

    # File-level schema_version is also strict (bool must not pass as 1).
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profiles.json"
        p.write_text(json.dumps({"schema_version": True, "profiles": {}}), encoding="utf-8")
        with pytest.raises(RuntimeError):
            load_profiles(p)
        # Break cache isolation: load_profiles is lru_cached per path arg,
        # distinct tmp paths above keep this deterministic.
        p2 = Path(td) / "profiles2.json"
        p2.write_text(json.dumps({"schema_version": 2, "profiles": {}}), encoding="utf-8")
        with pytest.raises(RuntimeError):
            load_profiles(p2)


def test_shell_event_rejects_legacy_only_command() -> None:
    import pytest

    from codey.agents.shell_approval import shell_command_event_fields

    with pytest.raises((ValueError, TypeError, KeyError)):
        shell_command_event_fields({"command": "echo hi", "cwd": "."})


def test_shell_event_rejects_tampered_digest() -> None:
    import pytest

    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    full = "echo hello"
    fields = shell_command_payload(full)
    record = {
        "command": full,
        "command_preview": fields["command"],
        "command_sha256": "0" * 64,  # tampered: does not match full text
        "command_chars": fields["command_chars"],
        "command_truncated": fields["command_truncated"],
    }
    with pytest.raises((ValueError, TypeError)):
        shell_command_event_fields(record)


def test_shell_event_rejects_tampered_preview_and_counts() -> None:
    import pytest

    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    full = "echo hello world"
    fields = shell_command_payload(full)
    # Wrong preview.
    bad_preview = dict(
        {
            "command": full,
            "command_preview": "WRONG",
            "command_sha256": fields["command_sha256"],
            "command_chars": fields["command_chars"],
            "command_truncated": fields["command_truncated"],
        }
    )
    with pytest.raises((ValueError, TypeError)):
        shell_command_event_fields(bad_preview)
    # Wrong chars.
    bad_chars = dict(
        {
            "command": full,
            "command_preview": fields["command"],
            "command_sha256": fields["command_sha256"],
            "command_chars": 9999,
            "command_truncated": fields["command_truncated"],
        }
    )
    with pytest.raises((ValueError, TypeError)):
        shell_command_event_fields(bad_chars)
    # Wrong truncated flag.
    bad_flag = dict(
        {
            "command": full,
            "command_preview": fields["command"],
            "command_sha256": fields["command_sha256"],
            "command_chars": fields["command_chars"],
            "command_truncated": not bool(fields["command_truncated"]),
        }
    )
    # Only rejects when the flag is actually wrong; for short commands the
    # negation is wrong, for long commands the negation is also wrong, so
    # this is always a mismatch by construction.
    with pytest.raises((ValueError, TypeError)):
        shell_command_event_fields(bad_flag)
    # Missing fields (use a long command so a missing preview cannot be
    # mistaken for a valid event shape; short commands have full == preview).
    long_full = "python -c \"print('" + ("y" * 1600) + "')\""
    long_fields = shell_command_payload(long_full)
    for missing in ("command_preview", "command_sha256", "command_chars", "command_truncated"):
        rec = {
            "command": long_full,
            "command_preview": long_fields["command"],
            "command_sha256": long_fields["command_sha256"],
            "command_chars": long_fields["command_chars"],
            "command_truncated": long_fields["command_truncated"],
        }
        del rec[missing]
        with pytest.raises((ValueError, TypeError, KeyError)):
            shell_command_event_fields(rec)


def test_shell_event_accepts_current_complete_record() -> None:
    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    full = "echo hello"
    fields = shell_command_payload(full)
    record = {
        "command": full,
        "command_preview": fields["command"],
        "command_sha256": fields["command_sha256"],
        "command_chars": fields["command_chars"],
        "command_truncated": fields["command_truncated"],
    }
    out = shell_command_event_fields(record)
    assert out["command"] == fields["command"]
    assert out["command_sha256"] == fields["command_sha256"]
    assert out["command_chars"] == fields["command_chars"]
    assert out["command_truncated"] == fields["command_truncated"]

    # Long command keeps truncation bound + full hash.
    long_cmd = "python -c \"print('" + ("x" * 1600) + "')\""
    long_fields = shell_command_payload(long_cmd)
    long_record = {
        "command": long_cmd,
        "command_preview": long_fields["command"],
        "command_sha256": long_fields["command_sha256"],
        "command_chars": long_fields["command_chars"],
        "command_truncated": long_fields["command_truncated"],
    }
    long_out = shell_command_event_fields(long_record)
    assert len(str(long_out["command"])) <= 1000
    assert long_out["command_truncated"] is True
    assert long_out["command_sha256"] == hashlib.sha256(long_cmd.encode()).hexdigest()


def test_mode_selection_trace_is_live_not_dead_code() -> None:
    from codey.runs.trace import RunTraceManifest, RunTraceRecorder

    manifest = RunTraceManifest(run_id="r1", session_id="s1")
    rec = RunTraceRecorder.__new__(RunTraceRecorder)
    rec.manifest = manifest
    # Bypass __init__ side effects; record_mode_selection only touches manifest + flush.
    # Monkey-patch flush to no-op for this unit lock.
    rec.flush = lambda: None  # type: ignore[method-assign]
    rec.path = None  # type: ignore[assignment]
    rec.disabled = False
    rec.record_mode_selection(
        baseline_mode="project",
        selected_mode="project",
        final_mode="project",
        source="baseline",
        reason_code="baseline_kept",
    )
    payload = manifest.to_payload()
    assert payload["mode_selection"]["final_mode"] == "project"
    assert payload["mode_final"] == "project"
    assert "router" not in payload
