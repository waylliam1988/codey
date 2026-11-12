(function (emit) {
// The protocol and user program share only guest objects. Lock the guest's
// intrinsics so user code cannot rewrite serialization or call accounting.
const intrinsicRoots = Reflect.ownKeys(globalThis)
  .filter(key => key !== "__emit" && key !== "globalThis")
  .map(key => Object.getOwnPropertyDescriptor(globalThis, key)?.value);
intrinsicRoots.push(
  Object.getPrototypeOf(async function () {}),
  Object.getPrototypeOf((function* () {})()),
  Object.getPrototypeOf((async function* () {})()),
  Object.getPrototypeOf([][Symbol.iterator]()),
  Object.getPrototypeOf(""[Symbol.iterator]()),
  Object.getPrototypeOf(new Map().entries()),
  Object.getPrototypeOf(new Set().values()),
  Object.getPrototypeOf("".matchAll(/x/g)),
);

function hardenIntrinsics() {
  const visited = new Set([globalThis]);
  function harden(value) {
    if ((typeof value !== "object" && typeof value !== "function") || value === null || visited.has(value)) return;
    visited.add(value);
    harden(Object.getPrototypeOf(value));
    for (const descriptor of Object.values(Object.getOwnPropertyDescriptors(value))) {
      harden(descriptor.value);
      harden(descriptor.get);
      harden(descriptor.set);
    }
    Object.freeze(value);
  }
  for (const value of intrinsicRoots) harden(value);
  harden(Date);
  Object.freeze(globalThis);
}

let active = null;
const pending = new Map();
let nextCall = 0;
let nextStateCall = 0;

function send(message) {
  emit(JSON.stringify(message));
}

function rejectConsole() {
  const error = new Error("Console output is unavailable. Return the required data with return; console.log/error do not produce a result.");
  error.code = "console_output_denied";
  throw error;
}

function unwrap(result) {
  if (result === null || typeof result !== "object" || typeof result.ok !== "boolean") {
    throw new Error("unwrap expected a tool result");
  }
  if (result.ok === false) {
    throw new Error(String(result.error ?? "tool call failed"));
  }
  if (result.ok !== true || !Object.prototype.hasOwnProperty.call(result, "value")) {
    throw new Error("malformed successful tool result");
  }
  return result.value;
}

function encodeJson(value, label, errorCode, properties = {}) {
  const locations = new WeakMap();
  return JSON.stringify(value, function (key, item) {
    const location = locations.has(this) ? locations.get(this) + "[" + JSON.stringify(key) + "]" : "$";
    if (item !== null && typeof item === "object") locations.set(item, location);
    if (item === undefined || ["function", "symbol", "bigint"].includes(typeof item)
        || (typeof item === "number" && !Number.isFinite(item))) {
      const kind = typeof item === "number" ? "non-finite number" : typeof item;
      const detail = item === undefined
        ? " A nonexistent property produces undefined. Follow each tool's declared value type; correct the expression."
        : "";
      const error = new Error(label + " must contain only JSON data: " + kind + " at " + location + "." + detail
        + " Use explicit null only for intentional empty values."
        + (properties[key]?.description ? " Field contract: " + properties[key].description : ""));
      error.code = errorCode;
      throw error;
    }
    return item;
  });
}

function checkedToolResult(value, name) {
  if (value === null || typeof value !== "object" || typeof value.ok !== "boolean") return value;
  return new Proxy(value, {
    get(target, property, receiver) {
      if (typeof property === "string" && !["then", "toJSON", "ok", "value", "error", "result_ref"].includes(property)
          && !Reflect.has(target, property)) {
        const hint = name === "read_file" && property === "base_result_ref"
          ? ". For whole-file edits, use readResult.result_ref, not readResult.value.result_ref."
          : "; use unwrap(result)." + property + " or result.value." + property + ". Receipt metadata result_ref stays on result.";
        const error = new Error("tools." + name + " returned Result<T>, not its data. Undeclared result field "
          + JSON.stringify(property) + hint);
        error.code = "result_contract";
        throw error;
      }
      return Reflect.get(target, property, receiver);
    },
  });
}

function toolsProxy(path = []) {
  const callable = (args = {}) => {
    const name = path.join(".");
    if (!active.catalog.has(name)) {
      throw new Error(`tool is not in the frozen catalog: ${name}`);
    }
    if (active.calls >= active.limits.maxChildCalls) {
      throw new Error("codemode child call limit exceeded");
    }
    if (active.pending >= active.limits.maxPendingPromises) {
      throw new Error("codemode pending promise limit exceeded");
    }
    if (name === "edit" && args !== null && typeof args === "object"
        && (Object.hasOwn(args, "read") || Object.hasOwn(args, "changes"))) {
      const ref = active.readReceipts.get(args.read);
      if (!ref || !Object.hasOwn(args, "read") || !Object.hasOwn(args, "changes") || Object.keys(args).length !== 2) {
        throw new Error("JSON edit requires {read, changes}. Pass the original tools.read_file result from this program as read. Do not unwrap read.");
      }
      args = {base_result_ref: ref, changes: args.changes};
    }
    const encodedArgs = encodeJson(args, "Arguments for tools." + name, "argument_encoding",
      active.catalog.get(name)?.parameters?.properties);
    const callId = `call-${++nextCall}`;
    active.calls += 1;
    active.pending += 1;
    const result = trackedCall(callId, false, name);
    send({ type: "call", callId, name, args: JSON.parse(encodedArgs) });
    return result;
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
  const result = trackedCall(callId, true);
  send({ type: "state", callId, op, key, ...(op === "store" ? { value } : {}) });
  return result;
}

function trackedCall(callId, state = false, name = "") {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  const record = { resolve, reject, state, name, observed: false };
  pending.set(callId, record);
  active.issued.set(callId, record);
  return Object.freeze({
    then(onFulfilled, onRejected) {
      record.observed = true;
      return promise.then(onFulfilled, onRejected);
    },
    catch(onRejected) {
      record.observed = true;
      return promise.catch(onRejected);
    },
    finally(onFinally) {
      record.observed = true;
      return promise.finally(onFinally);
    },
    get [Symbol.toStringTag]() { return "Promise"; },
  });
}

function unawaitedCallCount() {
  return [...active.issued.values()].filter((call) => !call.observed).length;
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
    catalog: new Map(catalog.map((item) => [String(item.name), item])),
    limits: {
      maxChildCalls: Number(limits.maxChildCalls ?? 64),
      maxConcurrentCalls: Number(limits.maxConcurrentCalls ?? 8),
      maxPendingPromises: Number(limits.maxPendingPromises ?? 64),
      maxOutputBytes: Number(limits.maxOutputBytes ?? 200000),
    },
    calls: 0,
    pending: 0,
    issued: new Map(),
    readReceipts: new WeakMap(),
    stateEnabled: Boolean(request.stateEnabled),
  };
  Object.assign(globalThis, {
    tools: toolsProxy(),
    unwrap,
    store: (key, value) => stateCall("store", String(key), value),
    load: (key) => stateCall("load", String(key)),
    console: Object.freeze({ log: rejectConsole, error: rejectConsole }),
  });
  // Fix the ambient clock and PRNG for one durable execution. Reconstructing
  // the same program consumes exactly the same sequence after a restart.
  const epoch = Number(request.determinism?.epochMs);
  const seed = Number(request.determinism?.seed) >>> 0;
  (0, eval)(`{
  globalThis.inputs = JSON.parse(${JSON.stringify(JSON.stringify(request.inputs))});
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
  }}`);
  let compiling = true;
  try {
    hardenIntrinsics();
    const script = (0, eval)(`(async () => {\n${code}\n})\n//# sourceURL=codemode-program.js`);
    compiling = false;
    const value = await script();
    if (value === undefined && pending.size === 0) {
      const invocation = active.calls === 0
        ? "No child tool was executed. code is a function body: declaring a helper/main does not call it. If you define main, end with return await main(); or put the awaited tool calls directly in code. "
        : "";
      const error = new Error("CodeMode returned undefined. " + invocation + "A missing return OR returning a nonexistent property produces undefined. Check the declared Result<T> shape: successful tool data is inside result.value. Return an explicit JSON value; use return null only for an intentional empty result.");
      error.code = "missing_return";
      throw error;
    }
    const encoded = encodeJson(value, "CodeMode result", "result_encoding");
    if (unescape(encodeURIComponent(encoded ?? "null")).length > active.limits.maxOutputBytes) {
      send({ type: "done", ok: false, errorCode: "output_limit", error: "codemode output exceeds output byte limit", childCalls: active.calls, unawaitedCalls: unawaitedCallCount() });
      return;
    }
    send({ type: "done", ok: true, value: JSON.parse(encoded), childCalls: active.calls, unawaitedCalls: unawaitedCallCount() });
  } catch (error) {
    const stack = String(error?.stack ?? "").replace(/<input>:(\d+)/g,
      (_match, line) => "codemode-program.js:" + Math.max(1, Number(line) - 1));
    const location = stack.match(/codemode-program\.js:(\d+)/);
    const sourceLines = code.split(/\r\n|\r|\n|\u2028|\u2029/);
    const sourceLine = location ? Math.min(sourceLines.length, Math.max(1, Number(location[1]))) : 1;
    const firstLine = Math.max(0, sourceLine - 2);
    const excerpt = sourceLines.slice(firstLine, firstLine + 3)
      .map((line, index) => `${firstLine + index + 1}: ${JSON.stringify(line.slice(0, 240))}`).join("\n");
    const diagnostic = compiling
      ? "JavaScript compilation failed after successful outer JSON decoding. No child tool was executed.\n"
        + String(error?.name ?? "Error") + ": " + String(error?.message ?? error).slice(0, 400) + "\n"
        + stack.split("\n").slice(0, 4).map(line => line.slice(0, 200)).join("\n")
        + "\nSource excerpt (JSON-escaped lines):\n" + excerpt
        + "\nPut multiline or quoted task text in inputs and reference inputs.<field> in code. Raw line breaks inside JS single/double-quoted literals are invalid."
      : String(error?.message ?? error);
    send({ type: "done", ok: false, errorCode: compiling ? "syntax_error" : String(error?.code ?? "execution_error"), error: diagnostic, childCalls: active.calls, unawaitedCalls: unawaitedCallCount() });
  } finally {
    active = null;
  }
}

return async (line) => {
  let message;
  try { message = JSON.parse(line); } catch { send({ type: "error", code: "protocol", message: "invalid request JSON" }); return; }
  if (message.type === "result" || message.type === "stateResult") {
    const waiter = pending.get(message.callId);
    if (!waiter) return;
    pending.delete(message.callId);
    if (active && message.type === "result") active.pending = Math.max(0, active.pending - 1);
    if (message.ok) {
      const result = waiter.state ? message.value : checkedToolResult(message.value, waiter.name);
      if (active && waiter.name === "read_file" && result?.ok === true && typeof result.result_ref === "string") {
        active.readReceipts.set(result, result.result_ref);
      }
      waiter.resolve(result);
    } else {
      const error = new Error(String(message.error ?? "tool call failed"));
      error.code = String(message.errorCode ?? "tool_error");
      waiter.reject(error);
    }
    return;
  }
  if (message.type === "execute") await execute(message);
};
})(__emit)
