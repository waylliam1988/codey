"""Execute the actual browser SSE runtime with deterministic transport events."""
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "codey/web/assets/sse.js"


@pytest.mark.parametrize("scenario", ["dedup_reset", "buffer_gap"])
def test_browser_cursor_and_reconcile_gap(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    script = r'''
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const handled = [], applied = [], pending = [], sources = [];
class EventSource { constructor(url) {this.url=url; sources.push(this);} }
EventSource.OPEN=1;
const window = {};
const context = vm.createContext({window, EventSource, setTimeout, clearTimeout,
  fetch: () => new Promise(resolve => pending.push(resolve))});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
window.CodeySse.init({handleServerEvent: e => handled.push(e), applyRunState: s => applied.push(s),
  setStatus:()=>{}, refreshProviderStatus:()=>{}});
window.CodeySse.connect();
const emit=(id,data)=>sources[0].onmessage({lastEventId:String(id),data:JSON.stringify(data)});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  if (process.argv[2]==='dedup_reset') {
    emit(9,{type:'tool'}); emit(9,{type:'tool'}); emit(8,{type:'tool'});
    assert.equal(handled.length,1,'duplicates/out-of-order events must be suppressed');
    emit(0,{type:'resync_required',cursor:0,reason:'sse_cursor_ahead'});
    emit(1,{type:'tool'}); emit(1,{type:'tool'});
    assert.deepEqual(handled.map(x=>x.type),['tool','resync_required','tool']);
  } else {
    const first = window.CodeySse.reconcileRunState();
    for(let i=1;i<=101;i++) emit(i,{type:'tool'});
    pending.shift()({ok:true,json:async()=>({n:1})});
    await first; await tick();
    assert.equal(handled.length,0,'incomplete buffered sequence must not be presented as complete');
    assert.equal(pending.length,1,'lost buffered events require a fresh state read');
    pending.shift()({ok:true,json:async()=>({n:2})}); await tick();
    assert.deepEqual(applied.map(x=>x.n),[1,2]);
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script, str(SOURCE), scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
