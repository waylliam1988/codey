import { readFileSync } from "node:fs";
import readline from "node:readline";
import { QuickJS, Intrinsics } from "./quickjs/index.js";

const wasm = readFileSync(new URL("./quickjs/quickjs.wasm", import.meta.url));
const prelude = readFileSync(new URL("./guest_runner.js", import.meta.url), "utf8");
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let machine;
let receive;
let deadline = Infinity;
let starting = false;

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function drain() {
  try {
    machine.executePendingJobs();
  } catch (error) {
    send({ type: "error", code: Date.now() >= deadline ? "timeout" : "execution_error", message: String(error) });
  }
}

rl.on("line", async (line) => {
  try {
    const message = JSON.parse(line);
    if (!machine) {
      if (starting || message.type !== "execute") throw new Error("expected one execution request");
      starting = true;
      deadline = Date.now() + Number(message.limits?.timeoutMs ?? 30000);
      machine = await QuickJS.create({
        wasm, memoryLimit: 64 * 1024 * 1024, maxStackSize: 256 * 1024,
        intrinsics: Intrinsics.ALL & ~Intrinsics.WEAK_REF,
        timezoneOffset: 0,
        interruptHandler: () => Date.now() >= deadline,
        wasi: (memory) => ({
          clock_time_get(_clock, _precision, pointer) {
            new DataView(memory.buffer).setBigUint64(pointer, BigInt(message.determinism?.epochMs ?? 0) * 1000000n, true);
            return 0;
          },
          random_get(pointer, length) {
            new Uint8Array(memory.buffer, pointer, length).fill(0);
            return 0;
          },
          fd_write(_fd, vectors, count, written) {
            const view = new DataView(memory.buffer);
            let length = 0;
            for (let i = 0; i < count; i++) length += view.getUint32(vectors + i * 8 + 4, true);
            view.setUint32(written, length, true);
            return 0;
          },
        }),
      });
      machine.withScope(() => {
        machine.setProp(machine.global, "__emit", machine.newFunction("emit", (payload) => {
          send(JSON.parse(payload.toString()));
          return machine.undefined;
        }));
      });
      receive = machine.evalCode(prelude, "codemode-prelude.js");
      machine.evalCode("delete globalThis.__emit").dispose();
    }
    machine.withScope(() => machine.callFunction(receive, machine.undefined, machine.newString(line)));
    drain();
  } catch (error) {
    send({ type: "error", code: Date.now() >= deadline ? "timeout" : "execution_error", message: String(error) });
  }
});
