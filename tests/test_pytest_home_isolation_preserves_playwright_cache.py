"""State-home isolation must not move Playwright's installed browser cache."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROBE = r'''
import json, os, runpy, subprocess, sys
from pathlib import Path
import playwright
import pytest

platform = sys.argv[1]
driver = Path(playwright.__file__).parent / "driver"
node = driver / ("node.exe" if os.name == "nt" else "node")
registry = driver / "package/lib/server/registry/index.js"
js = """
Object.defineProperty(process, "platform", {value: process.argv[2]});
require("os").homedir = () => process.env.HOME;
console.log(require(process.argv[1]).registryDirectory);
"""

def browser_cache():
    result = subprocess.run([str(node), "-e", js, str(registry), platform],
                            capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.strip()

before = browser_cache()
native_platform = sys.platform
sys.platform = platform
runpy.run_path("tests/conftest.py")
after = browser_cache()
# Reinitialization must preserve the original cache, not pin the temporary home.
runpy.run_path("tests/conftest.py")
reinitialized = browser_cache()
sys.platform = native_platform
child = subprocess.run([sys.executable, "-c",
    "import json,os; print(json.dumps({k: os.environ.get(k) for k in "
    "('HOME', 'PLAYWRIGHT_BROWSERS_PATH')}))"],
    capture_output=True, text=True, timeout=30, check=True)
print(json.dumps({"before": before, "after": after,
    "reinitialized": reinitialized, "home": str(Path.home()),
    "state_home": os.environ["PYTEST_STATE_HOME"],
    "cache_setting": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
    "child": json.loads(child.stdout)}))
'''


@pytest.mark.parametrize(("platform", "cache_mode"), [
    ("linux", "default"),
    ("darwin", "default"),
    ("win32", "default"),
    ("win32", "localappdata"),
    ("linux", "xdg"),
    ("linux", "empty"),
    ("linux", "explicit"),
    ("linux", "relative"),
    ("linux", "package-local"),
    ("linux", "npm-config"),
    ("linux", "npm-package"),
    ("linux", "empty-overrides-npm"),
])
def test_bootstrap_keeps_real_playwright_cache_and_isolates_state(tmp_path, platform, cache_mode):
    env = dict(os.environ)
    for name in ("PYTEST_STATE_HOME", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME", "LOCALAPPDATA",
                 "npm_config_playwright_browsers_path", "npm_package_config_playwright_browsers_path"):
        env.pop(name, None)
    original_home = tmp_path / "original-home"
    original_home.mkdir()
    state_home = tmp_path / "isolated-state"
    env.update(HOME=str(original_home), USERPROFILE=str(original_home), PYTEST_STATE_HOME=str(state_home))
    if cache_mode == "localappdata":
        env["LOCALAPPDATA"] = str(tmp_path / "browser-appdata")
    if cache_mode == "xdg":
        env["XDG_CACHE_HOME"] = str(tmp_path / "browser-cache")
    explicit = {"empty": "", "explicit": str(tmp_path / "custom-browsers"),
                "relative": "relative-browsers", "package-local": "0"}.get(cache_mode)
    if explicit is not None:
        env["PLAYWRIGHT_BROWSERS_PATH"] = explicit
    if cache_mode in {"npm-config", "empty-overrides-npm"}:
        env["npm_config_playwright_browsers_path"] = str(tmp_path / "npm-browsers")
    if cache_mode == "npm-package":
        env["npm_package_config_playwright_browsers_path"] = "0"
    if cache_mode == "empty-overrides-npm":
        env["PLAYWRIGHT_BROWSERS_PATH"] = ""

    result = subprocess.run([sys.executable, "-c", _PROBE, platform], cwd=_REPO,
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["after"] == observed["before"], observed
    assert observed["reinitialized"] == observed["before"], observed
    assert Path(observed["home"]).resolve() == state_home.resolve()
    assert Path(observed["child"]["HOME"]).resolve() == state_home.resolve()
    assert observed["child"]["PLAYWRIGHT_BROWSERS_PATH"] == observed["cache_setting"]
    if explicit:
        assert observed["cache_setting"] == explicit
