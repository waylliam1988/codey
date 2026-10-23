import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.control.protocol import ControlError
from codey.launcher import releases, validation_cache
from codey.storage.local_store import write_json_atomic
from tools.publish_runtime import publish_runtime


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "codey/web").mkdir(parents=True)
    (source / "codey/web/index.html").write_text("built")
    (source / "codey/__main__.py").write_text("print('Codey')\n")
    (source / "pyproject.toml").write_text('[project]\nname="codey"\nrequires-python=">=3.11"\ndependencies=[]\n')
    publish_runtime(source, source / "pyproject.toml", version="test")
    home = tmp_path / "state"
    active = releases.snapshot(home, source)
    write_json_atomic(home / "active-release.json", active)
    env = {"value": "python-and-dependencies-one"}
    monkeypatch.setattr(validation_cache, "environment_digest", lambda: env["value"])
    calls, database_values = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "-c" and "LocalService" in argv[2]:
            database = kwargs["cwd"]  # The probe must use its isolated copy, never live data.
            assert str(database).startswith(str(home / "releases"))
            probe = Path(argv[3]) / "sample.sqlite3"
            if probe.exists():
                with sqlite3.connect(probe) as db:
                    database_values.append(db.execute("SELECT value FROM sample").fetchone()[0])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(releases.subprocess, "run", run)
    service = SimpleNamespace(ctx=SimpleNamespace(state_home=home))
    def prepare(request, **extra):
        return releases.prepare(service, {"request_id": request, "source": str(source), **extra})
    return home, source, env, calls, database_values, prepare


def test_static_cache_reuses_only_code_checks_and_always_probes_current_database(preparation):
    home, _, _, calls, values, prepare = preparation
    with sqlite3.connect(home / "sample.sqlite3") as db:
        db.execute("CREATE TABLE sample(value INTEGER)")
        db.execute("INSERT INTO sample VALUES(1)")
    first = prepare("first")
    with sqlite3.connect(home / "sample.sqlite3") as db:
        db.execute("UPDATE sample SET value=2")
    second = prepare("second")
    assert len(calls) == 4  # Two static checks, plus two fresh state preflights.
    assert values == [1, 2]
    assert [row["state"] for row in second["verification"]] == ["cached", "cached", "settled"]
    assert all("elapsed_seconds" in row and "finished_at" in row for row in first["verification"])
    assert prepare("second") == second
    assert len(calls) == 4  # Exact request replay is observational/idempotent.


@pytest.mark.parametrize("change", ["environment", "rules", "source", "corrupt-cache"])
def test_cache_invalidation_reexecutes_static_checks(preparation, monkeypatch, change):
    home, source, env, calls, _, prepare = preparation
    prepare("first")
    if change == "environment":
        env["value"] = "new-python-or-dependency"
    elif change == "rules":
        monkeypatch.setattr(validation_cache, "VALIDATION_RULES", "new-validation-rules")
    elif change == "source":
        (source / "codey/__main__.py").write_text("print('new code')\n")
    else:
        next((home / "validation-cache").glob("*.json")).write_text("not JSON")
    prepare("second")
    assert len(calls) == 6


def test_custom_verification_is_never_cached(preparation):
    _, _, _, calls, _, prepare = preparation
    custom = [["explicit-validator", "run"]]
    prepare("first", verification=custom)
    prepare("second", verification=custom)
    assert calls.count(custom[0]) == 2


def test_failed_checks_are_not_cached_or_repeated_under_the_same_request(preparation, monkeypatch):
    home, _, _, calls, _, prepare = preparation
    def failed(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(releases.subprocess, "run", failed)
    for _ in range(2):
        with pytest.raises(ControlError, match="validation failed"):
            prepare("failed")
    assert len(calls) == 1
    assert not list((home / "validation-cache").glob("*.json"))


def test_changed_environment_blocks_activation_of_old_validation(preparation):
    home, _, env, _, _, prepare = preparation
    result = prepare("first")
    env["value"] = "changed"
    service = SimpleNamespace(ctx=SimpleNamespace(state_home=home), instance_id="instance", allowed_roots=())
    with pytest.raises(ControlError, match="environment changed"):
        releases.activate(service, {"request_id": "activation", "candidate_release": result["release_id"],
            "candidate_digest": result["candidate_digest"], "expected_instance_id": "instance"})
    assert not list((home / "upgrades").glob("*.json"))


def test_cache_rejects_a_failed_or_partial_evidence_record(preparation):
    home, _, _, calls, _, prepare = preparation
    prepare("first")
    cache = next((home / "validation-cache").glob("*.json"))
    data = json.loads(cache.read_text())
    data["verification"][0]["exit_code"] = 1
    cache.write_text(json.dumps(data))
    prepare("second")
    assert len(calls) == 6


@pytest.mark.parametrize("changed", ["version", "metadata", "python", "environment"])
def test_environment_fingerprint_tracks_real_interpreter_and_dependency_inputs(monkeypatch, changed):
    distribution = SimpleNamespace(metadata={"Name": "test-dependency"}, version="1.0", read_text=lambda _: "original")
    monkeypatch.setattr(validation_cache.importlib.metadata, "distributions", lambda: [distribution])
    before = validation_cache.environment_digest()
    if changed == "version":
        distribution.version = "2.0"
    elif changed == "metadata":
        distribution.read_text = lambda _: "changed installed RECORD"
    elif changed == "python":
        monkeypatch.setattr(validation_cache.sys, "version", validation_cache.sys.version + " changed")
    else:
        monkeypatch.setenv("PYTHONOPTIMIZE", "new-value")
    assert validation_cache.environment_digest() != before


def test_unwritable_optional_cache_still_validates_and_runs_state_preflight(preparation, monkeypatch):
    _, _, _, calls, _, prepare = preparation
    def unwritable(*args):
        raise PermissionError("cache is read-only")
    monkeypatch.setattr(releases, "save_checks", unwritable)
    assert prepare("first")["status"] == "validated"
    assert prepare("second")["status"] == "validated"
    assert len(calls) == 6


def test_static_validator_mutation_never_creates_reusable_evidence(preparation, monkeypatch):
    home, source, _, _, _, prepare = preparation
    original = (source / "codey/__main__.py").read_text()
    def modifies(argv, **kwargs):
        (Path(kwargs["cwd"]) / "codey/__main__.py").write_text("modified validation copy")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(releases.subprocess, "run", modifies)
    with pytest.raises(ControlError, match="modified"):
        prepare("mutates")
    assert not list((home / "validation-cache").glob("*.json"))
    assert (source / "codey/__main__.py").read_text() == original


def test_environment_change_during_validation_never_creates_cache_evidence(preparation, monkeypatch):
    home, _, _, _, _, prepare = preparation
    digests = iter(["before", "after"])
    monkeypatch.setattr(validation_cache, "environment_digest", lambda: next(digests))
    with pytest.raises(ControlError, match="environment changed"):
        prepare("environment-race")
    assert not list((home / "validation-cache").glob("*.json"))
