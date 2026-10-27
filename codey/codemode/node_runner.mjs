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

function rejectConsole() {
  const error = new Error("Console output is unavailable. Return the required data with return; console.log/error do not produce a result.");
  error.code = "console_output_denied";
  throw error;
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
    send({ type: "done", ok: false, errorCode: "sandbox_denied", error: "direct host access is denied", childCalls: 0, unawaitedCalls: 0 });
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
  };
  const context = vm.createContext({
    tools: toolsProxy(),
    store: (key, value) => stateCall("store", String(key), value),
    load: (key) => stateCall("load", String(key)),
    console: Object.freeze({ log: rejectConsole, error: rejectConsole }),
  }, { codeGeneration: { strings: false, wasm: false } });
  // Fix the ambient clock and PRNG for one durable execution. Reconstructing
  // the same program consumes exactly the same sequence after a restart.
  const epoch = Number(request.determinism?.epochMs);
  const seed = Number(request.determinism?.seed) >>> 0;
  new vm.Script(`
  const inputs = JSON.parse(${JSON.stringify(JSON.stringify(request.inputs))});
  (function freezeInputs(value) {
    if (value && typeof value === "object") {
      for (const item of Object.values(value)) freezeInputs(item);
      Object.freeze(value);
    }
  })(inputs);
  {
    const OriginalDate = Date;
    const epoch = ${JSON.stringify(epoch)};
    OriginalDate.now = () => epoch;
    Date = new Proxy(OriginalDate, {
      apply() { return new OriginalDate(epoch).toString(); },
      construct(target, args, newTarget) {
        return Reflect.construct(target, args.length ? args : [epoch], newTarget);
      },
    });
    let state = ${JSON.stringify(seed)};
    Math.random = () => { state = (Math.imul(1664525, state) + 1013904223) >>> 0; return state / 4294967296; };
    Object.freeze(Math);
    Object.freeze(Date);
    WeakRef = undefined;
    FinalizationRegistry = undefined;
    SharedArrayBuffer = undefined;
    Atomics = undefined;
  }`).runInContext(context);
  let compiling = true;
  try {
    const script = new vm.Script(`(async () => {\n${code}\n})()`, { filename: "codemode-program.js", lineOffset: -1 });
    compiling = false;
    const value = await script.runInContext(context, { timeout: 1_000 });
    if (value === undefined && pending.size === 0) {
      const invocation = active.calls === 0
        ? "No child tool was executed. code is a function body: declaring a helper/main does not call it. If you define main, end with return await main(); or put the awaited tool calls directly in code. "
        : "";
      const error = new Error("CodeMode returned undefined. " + invocation + "A missing return OR returning a nonexistent property produces undefined. Check the declared Result<T> shape: successful tool data is inside result.value. Return an explicit JSON value; use return null only for an intentional empty result.");
      error.code = "missing_return";
      throw error;
    }
    const locations = new WeakMap();
    const encoded = JSON.stringify(value, function (key, item) {
      const location = locations.has(this) ? locations.get(this) + "[" + JSON.stringify(key) + "]" : "$";
      if (item !== null && typeof item === "object") locations.set(item, location);
      if (item === undefined || ["function", "symbol", "bigint"].includes(typeof item)
          || (typeof item === "number" && !Number.isFinite(item))) {
        const kind = typeof item === "number" ? "non-finite number" : typeof item;
        const detail = item === undefined
          ? " A nonexistent property produces undefined. Follow each tool's declared value type; correct the returned expression."
          : "";
        const error = new Error("CodeMode result must contain only JSON data: " + kind + " at " + location + "." + detail + " Use explicit null only for intentional empty values.");
        error.code = "result_encoding";
        throw error;
      }
      return item;
    });
    if (Buffer.byteLength(encoded ?? "null", "utf8") > active.limits.maxOutputBytes) {
      send({ type: "done", ok: false, errorCode: "output_limit", error: "codemode output exceeds output byte limit", childCalls: active.calls, unawaitedCalls: pending.size });
      return;
    }
    send({ type: "done", ok: true, value: JSON.parse(encoded), childCalls: active.calls, unawaitedCalls: pending.size });
  } catch (error) {
    const diagnostic = compiling
      ? "JavaScript compilation failed after successful outer JSON decoding. No child tool was executed.\n"
        + String(error?.stack ?? error).split("\n").slice(0, 4).map(line => line.slice(0, 200)).join("\n")
        + "\nPut multiline or quoted task text in inputs and reference inputs.<field> in code. Raw line breaks inside JS single/double-quoted literals are invalid."
      : String(error?.message ?? error);
    send({ type: "done", ok: false, errorCode: compiling ? "syntax_error" : String(error?.code ?? "execution_error"), error: diagnostic, childCalls: active.calls, unawaitedCalls: pending.size });
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
