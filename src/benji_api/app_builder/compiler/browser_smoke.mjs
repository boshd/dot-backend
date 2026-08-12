import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import QUICKJS_RELEASE_SYNC from "@jitl/quickjs-wasmfile-release-sync";
import * as esbuild from "esbuild";
import { newQuickJSWASMModuleFromVariant } from "quickjs-emscripten-core";

const MAX_INPUT_BYTES = 3_600_000;
const MAX_ERRORS = 12;
const MEMORY_LIMIT_BYTES = 96 * 1024 * 1024;
const STACK_LIMIT_BYTES = 1024 * 1024;
const MAX_EVENT_LOOP_TURNS = 80;

function respond(value, code = 0) {
  process.stdout.write(JSON.stringify(value));
  process.exitCode = code;
}

async function input() {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_INPUT_BYTES) throw new Error("smoke input exceeds 3.6 MB");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || value.protocol_version !== 1 || !value.bundle) throw new Error("invalid input");
  if (value.bundle.format !== "iife" || typeof value.bundle.javascript !== "string") {
    throw new Error("invalid browser bundle");
  }
  return value;
}

function issue(code, message) {
  return { code, message: String(message).slice(0, 800) };
}

function guestError(vm, handle) {
  let dumped;
  try {
    dumped = vm.dump(handle);
  } catch {
    dumped = "QuickJS guest failed";
  }
  if (dumped && typeof dumped === "object") {
    return String(dumped.stack || dumped.message || JSON.stringify(dumped));
  }
  return String(dumped);
}

function evaluate(vm, code, filename) {
  const result = vm.evalCode(code, filename);
  if (result.error) {
    const message = guestError(vm, result.error);
    result.error.dispose();
    throw new Error(message);
  }
  result.value.dispose();
}

function callNumber(vm, handle, value) {
  const argument = value === undefined ? null : vm.newString(value);
  try {
    const result = argument
      ? vm.callFunction(handle, vm.undefined, argument)
      : vm.callFunction(handle, vm.undefined);
    if (result.error) {
      const message = guestError(vm, result.error);
      result.error.dispose();
      throw new Error(message);
    }
    const output = vm.getNumber(result.value);
    result.value.dispose();
    return output;
  } finally {
    argument?.dispose();
  }
}

function callString(vm, handle, value) {
  const argument = value === undefined ? null : vm.newString(value);
  try {
    const result = argument
      ? vm.callFunction(handle, vm.undefined, argument)
      : vm.callFunction(handle, vm.undefined);
    if (result.error) {
      const message = guestError(vm, result.error);
      result.error.dispose();
      throw new Error(message);
    }
    const output = vm.getString(result.value);
    result.value.dispose();
    return output;
  } finally {
    argument?.dispose();
  }
}

function drainGuest(runtime, vm, stepHandle) {
  for (let turn = 0; turn < MAX_EVENT_LOOP_TURNS; turn += 1) {
    const pending = runtime.executePendingJobs(100);
    if (pending.error) {
      const message = guestError(pending.error.context, pending.error);
      pending.error.dispose();
      throw new Error(message);
    }
    const jobs = pending.value;
    pending.dispose();
    const timers = callNumber(vm, stepHandle);
    if (jobs === 0 && timers === 0 && !runtime.hasPendingJob()) return;
  }
  throw new Error("guest event loop did not settle");
}

async function buildGuest() {
  const result = await esbuild.build({
    entryPoints: [fileURLToPath(new URL("./smoke_guest.mjs", import.meta.url))],
    bundle: true,
    write: false,
    format: "iife",
    platform: "browser",
    target: "es2020",
    minify: false,
    banner: {
      js: `globalThis.atob ||= (input) => {
        const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let bits = 0, value = 0, output = "";
        for (const char of String(input).replace(/=+$/, "")) {
          const index = alphabet.indexOf(char);
          if (index < 0) continue;
          value = (value << 6) | index;
          bits += 6;
          if (bits >= 8) { bits -= 8; output += String.fromCharCode((value >> bits) & 255); }
        }
        return output;
      };`,
    },
    legalComments: "none",
    sourcemap: false,
    logLevel: "silent",
  });
  const output = result.outputFiles[0];
  if (!output) throw new Error("could not bundle the QuickJS smoke guest");
  return output.text;
}

function classifyFailure(error, deadline) {
  const message = String(error?.message || error);
  if (Date.now() >= deadline - 10 || /interrupted|event loop did not settle/i.test(message)) {
    return issue("browser_smoke_timeout", "app exceeded its isolated execution deadline");
  }
  if (/out of memory|allocation failed|memory limit/i.test(message)) {
    return issue("browser_smoke_memory_limit", "app exceeded its isolated memory limit");
  }
  return issue("browser_runtime_error", message);
}

let runtime;
let vm;
let initializeHandle;
let stepHandle;
let resultHandle;
let deadline = Date.now() + 10_000;
try {
  const request = await input();
  const requestedTimeout = Math.min(Math.max(Number(request.timeout_ms) || 8_000, 1_000), 10_000);
  deadline = Date.now() + requestedTimeout;
  const QuickJS = await newQuickJSWASMModuleFromVariant(QUICKJS_RELEASE_SYNC);
  runtime = QuickJS.newRuntime();
  runtime.setMemoryLimit(MEMORY_LIMIT_BYTES);
  runtime.setMaxStackSize(STACK_LIMIT_BYTES);
  runtime.setInterruptHandler(() => Date.now() >= deadline);
  vm = runtime.newContext();

  evaluate(vm, await buildGuest(), "dot-smoke-guest.js");
  initializeHandle = vm.getProp(vm.global, "__dotSmokeInitialize");
  stepHandle = vm.getProp(vm.global, "__dotSmokeStep");
  resultHandle = vm.getProp(vm.global, "__dotSmokeResult");
  evaluate(vm, `(() => {
    delete globalThis.__dotSmokeInitialize;
    delete globalThis.__dotSmokeStep;
    delete globalThis.__dotSmokeClick;
    delete globalThis.__dotSmokeAccept;
    delete globalThis.__dotSmokeFind;
    delete globalThis.__dotSmokeControlValue;
    delete globalThis.__dotSmokeResult;
    for (const name of [
      "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "Worker", "SharedWorker",
      "BroadcastChannel", "WebAssembly", "process", "require", "module", "Deno", "Bun",
      "indexedDB", "localStorage", "sessionStorage", "caches", "SharedArrayBuffer"
    ]) {
      try { delete globalThis[name]; } catch { globalThis[name] = undefined; }
      try { delete window[name]; } catch { window[name] = undefined; }
    }
  })()`, "dot-smoke-lockdown.js");
  const runtimeNonce = `dot_${randomUUID().replaceAll("-", "_")}`;
  callNumber(vm, initializeHandle, JSON.stringify({
    runtime_nonce: runtimeNonce,
    required_acceptance: [],
  }));
  evaluate(vm, request.bundle.javascript, "dot-generated-app.js");
  drainGuest(runtime, vm, stepHandle);

  const result = JSON.parse(callString(vm, resultHandle));
  if (!result || typeof result !== "object") throw new Error("guest returned invalid results");
  if (result.ok !== true) {
    const issues = Array.isArray(result.issues) ? result.issues.slice(0, MAX_ERRORS) : [];
    respond({ ok: false, issues }, 1);
  } else {
    respond({
      ok: true,
      result: {
        ...result.result,
        runtime: "quickjs-wasm",
        memory_limit_bytes: MEMORY_LIMIT_BYTES,
        stack_limit_bytes: STACK_LIMIT_BYTES,
      },
    });
  }
} catch (error) {
  respond({ ok: false, issues: [classifyFailure(error, deadline)] }, 1);
} finally {
  try { initializeHandle?.dispose(); } catch {}
  try { stepHandle?.dispose(); } catch {}
  try { resultHandle?.dispose(); } catch {}
  try { vm?.dispose(); } catch {}
  try { runtime?.dispose(); } catch {}
}
