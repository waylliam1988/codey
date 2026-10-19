import vm from "node:vm";
import readline from "node:readline";

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let active = null;
const pending = new Map();
let nextCall = 0;
let nextStateCall = 0;

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function jsonValue(value) {
  try {
    JSON.stringify(value);
    return value;
  } catch {
    return String(value);
  }
}

function toolsProxy(path = []) {
  const callable = async (args = {}) => {
    const name = path.join(".");
    if (!active.allowed.has(name)) {
      throw new Error(`tool is not in the frozen catalog: ${name}`);
    }
    if (active.calls >= active.limits.maxChildCalls) {
      throw new Error("codemode child call limit exceeded");
    }
    if (active.pending >= active.limits.maxPendingPromises) {
      throw new Error("codemode pending promise limit exceeded");
    }
    const callId = `call-${++nextCall}`;
    active.calls += 1;
    active.pending += 1;
    send({ type: "call", callId, name, args });
    return await new Promise((resolve, reject) => pending.set(callId, { resolve, reject }));
  };
  return new Proxy(callable, {
    get(_target, property) {
      if (typeof property === "symbol") return undefined;
      if (property === "then" && path.length === 0) return undefined;
      return toolsProxy([...path, String(property)]);
    },
    apply(_target, _thisArg, args) {
      return callable(args[0] ?? {});
    },
  });
}

function stateCall(op, key, value) {
  if (!active?.stateEnabled) {
    return Promise.reject(new Error("codemode state is unavailable"));
  }
  const callId = `state-${++nextStateCall}`;
  send({ type: "state", callId, op, key, ...(op === "store" ? { value } : {}) });
  return new Promise((resolve, reject) => pending.set(callId, { resolve, reject, state: true }));
}

function deniedSource(code) {
  return /\b(?:process|require|import|fetch|eval|globalThis|__dirname|__filename|child_process|fs|constructor)\b/i.test(code)
    || /\bFunction\s*\(/.test(code)
    || /tools\s*(?:\.\s*codemode|\[\s*["']codemode["']\s*\])/i.test(code);
}

async function execute(request) {
  const code = String(request.code ?? "");
  if (deniedSource(code)) {
    send({ type: "done", ok: false, errorCode: "sandbox_denied", error: "direct host access is denied" });
    return;
  }
  const catalog = Array.isArray(request.catalog) ? request.catalog : [];
  const limits = request.limits ?? {};
  active = {
    allowed: new Set(catalog.map((item) => String(item.name))),
    limits: {
      maxChildCalls: Number(limits.maxChildCalls ?? 64),
      maxConcurrentCalls: Number(limits.maxConcurrentCalls ?? 8),
      maxPendingPromises: Number(limits.maxPendingPromises ?? 64),
      maxOutputBytes: Number(limits.maxOutputBytes ?? 200000),
    },
    calls: 0,
    pending: 0,
    stateEnabled: Boolean(request.stateEnabled),
    storeWrites: {},
  };
  const context = vm.createContext({
    tools: toolsProxy(),
    store: (key, value) => {
      active.storeWrites[String(key)] = value;
      return stateCall("store", String(key), value);
    },
    load: (key) => stateCall("load", String(key)),
    console: Object.freeze({ log() {}, error() {} }),
  });
  try {
    const script = new vm.Script(`(async () => {\n${code}\n})()`, { filename: "codemode-program.js" });
    const value = await script.runInContext(context, { timeout: 1_000 });
    const encoded = JSON.stringify(jsonValue(value));
    if (Buffer.byteLength(encoded ?? "null", "utf8") > active.limits.maxOutputBytes) {
      send({ type: "done", ok: false, errorCode: "output_limit", error: "codemode output exceeds output byte limit", childCalls: active.calls });
      return;
    }
    send({ type: "done", ok: true, value: jsonValue(value), childCalls: active.calls, storeWrites: active.storeWrites });
  } catch (error) {
    send({ type: "done", ok: false, errorCode: String(error?.code ?? "execution_error"), error: String(error?.message ?? error), childCalls: active.calls });
  } finally {
    active = null;
  }
}

rl.on("line", async (line) => {
  let message;
  try { message = JSON.parse(line); } catch { send({ type: "error", code: "protocol", message: "invalid request JSON" }); return; }
  if (message.type === "result" || message.type === "stateResult") {
    const waiter = pending.get(message.callId);
    if (!waiter) return;
    pending.delete(message.callId);
    if (active && message.type === "result") active.pending = Math.max(0, active.pending - 1);
    if (message.ok) waiter.resolve(message.value); else {
      const error = new Error(String(message.error ?? "tool call failed"));
      error.code = String(message.errorCode ?? "tool_error");
      waiter.reject(error);
    }
    return;
  }
  if (message.type === "execute") await execute(message);
});
