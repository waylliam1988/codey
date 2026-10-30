"""Real WASM executions across capability and resource boundaries."""
from pathlib import Path

import pytest

from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits, ToolCatalogEntry


@pytest.mark.parametrize("code", [
    "return Object.getPrototypeOf(store)['con'+'structor']('return pro'+'cess')();",
    "return (await tools.echo({}))['con'+'structor']['con'+'structor']('return pro'+'cess')();",
    "return Object.getPrototypeOf(console.log)['con'+'structor']('return pro'+'cess')();",
    "try { await tools.echo({}); } catch (e) { return e['con'+'structor']['con'+'structor']('return pro'+'cess')(); }",
    "return ({} )['con'+'structor']['con'+'structor']('return fe'+'tch')();",
])
def test_obfuscated_host_access_cannot_escape_wasm(code):
    def invoke(*_):
        raise ValueError("ordinary host error")

    result = NodeWorkerRuntime().execute(code, catalog=(ToolCatalogEntry("echo", "echo"),), invoke=invoke)
    assert not result.ok and result.error_code != "sandbox_denied"


def test_guest_result_objects_belong_to_guest_and_host_globals_are_absent():
    result = NodeWorkerRuntime().execute(
        "const x = await tools.echo({}); return {owned:Object.getPrototypeOf(x) === Object.prototype, "
        "missing:['pro'+'cess','re'+'quire','fe'+'tch','Buffer','__emit'].every(k => typeof this[k] === 'undefined')};",
        catalog=(ToolCatalogEntry("echo", "echo"),), invoke=lambda *_: {"value": 1})
    assert result.ok and result.value == {"owned": True, "missing": True}


@pytest.mark.parametrize("mutation", [
    "JSON.stringify = () => '{\"type\":\"done\",\"ok\":true,\"value\":\"forged\"}';",
    "Map.prototype.values = () => [];",
    "Array.prototype.filter = () => [];",
    "Object.prototype.toJSON = () => ({type:'done',ok:true,value:'forged'});",
    "this.JSON = {parse:JSON.parse,stringify:() => '{}'};",
    "Object.getPrototypeOf(new Map().values()).next = () => ({done:true});",
])
def test_guest_cannot_mutate_protocol_intrinsics(mutation):
    result = NodeWorkerRuntime().execute(
        mutation + " tools.echo({}); return 'original';",
        catalog=(ToolCatalogEntry("echo", "echo"),), invoke=lambda *_: {"value": 1})
    assert not result.ok
    assert result.value != "forged"


@pytest.mark.parametrize("code", ["while(true) {}", "await Promise.resolve(); while(true) {}",
                                  "let x=[]; while(true) x.push(new Array(100000).fill(1));"])
def test_cpu_continuations_and_memory_are_bounded(code):
    result = NodeWorkerRuntime().execute(code, catalog=(), invoke=lambda *_: pytest.fail("unexpected tool"),
                                        limits=CodeModeLimits(timeout_ms=1500))
    assert not result.ok


def test_every_execution_asset_is_bound_by_recovery_contract(monkeypatch):
    original = Path.read_bytes
    first = NodeWorkerRuntime().contract_digest()

    def changed(path):
        data = original(path)
        return data + b"changed" if path.name == "quickjs.wasm" else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert NodeWorkerRuntime().contract_digest() != first
