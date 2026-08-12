import fs from "node:fs";
import { chromium } from "playwright-core";

const MAX_INPUT_BYTES = 3_600_000;
const MAX_OUTPUT_BYTES = 512_000;
const MAX_STEPS = 24;
const MAX_OPERATIONS = 100;

function respond(value, code = 0) {
  const encoded = JSON.stringify(value);
  process.stdout.write(encoded.length <= MAX_OUTPUT_BYTES
    ? encoded
    : JSON.stringify({
        ok: false,
        issues: [issue("real_browser_output_too_large", "real browser output exceeded 512 KB")],
      }));
  process.exitCode = code;
}

function issue(code, message) {
  return { code, message: String(message).slice(0, 800) };
}

function fail(code, message) {
  const error = new Error(message);
  error.code = code;
  throw error;
}

async function input() {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_INPUT_BYTES) fail("real_browser_input_too_large", "input exceeds 3.6 MB");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || value.protocol_version !== 1 || !value.bundle) {
    fail("real_browser_invalid_input", "invalid real browser request");
  }
  if (
    value.bundle.format !== "iife" ||
    value.bundle.sdk_version !== "2" ||
    typeof value.bundle.javascript !== "string" ||
    typeof value.bundle.css !== "string"
  ) {
    fail("real_browser_invalid_bundle", "real browser acceptance requires an SDK v2 IIFE bundle");
  }
  return value;
}

function executablePath() {
  const candidates = [
    process.env.DOT_APP_CHROMIUM_PATH,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  return candidates.find((candidate) => candidate && fs.existsSync(candidate));
}

function scriptLiteral(value) {
  return JSON.stringify(value)
    .replaceAll("</script", "<\\/script")
    .replaceAll("<!--", "<\\!--");
}

function styleText(value) {
  return String(value).replace(/<\/style/gi, "<\\/style").replaceAll("<!--", "<\\!--");
}

function iframeDocument(bundle, context, channelToken) {
  const bootstrap = `
    (() => {
      const channelToken = ${scriptLiteral(channelToken)};
      const bundleCode = ${scriptLiteral(bundle.javascript)};
      const queuedMessages = [];
      const readOnlyOperations = new Set(["app.data.get", "records.list"]);
      const call = Reflect.apply.bind(Reflect.apply);
      const defer = queueMicrotask.bind(window);
      const expireGesture = setTimeout.bind(window);
      let gestureSequence = 0;
      const newGestureId = () => "gesture_" + Date.now() + "_" + (++gestureSequence);
      const portPostMessage = MessagePort.prototype.postMessage;
      let hostPort = null;
      let appStarted = false;
      let currentGesture = null;

      const relay = (message) => {
        if (hostPort) call(portPostMessage, hostPort, [message]);
        else queuedMessages.push(message);
      };
      const reportError = (value) => {
        const error = value instanceof Error ? value : new Error(String(value || "app failed"));
        relay({
          type: "dot.app.error",
          protocol_version: 1,
          sdk_version: "2",
          channel_token: channelToken,
          error: { name: error.name, message: error.message },
        });
      };
      const issueGesture = (event) => {
        if (!event.isTrusted) return;
        if (event.type === "submit" && currentGesture) return;
        const gesture = { id: newGestureId(), used: false };
        currentGesture = gesture;
        expireGesture(() => {
          if (currentGesture === gesture) currentGesture = null;
        }, 0);
      };
      for (const eventName of ["click", "submit", "change"]) {
        document.addEventListener(eventName, issueGesture, true);
      }

      const receiveGuestMessage = (message) => {
        if (!message || typeof message !== "object") return;
        if (
          message.channel_token !== channelToken ||
          message.protocol_version !== 1 ||
          message.sdk_version !== "2"
        ) return;
        const outgoing = { ...message };
        delete outgoing.gesture_id;
        if (outgoing.type === "dot.app.request" && !readOnlyOperations.has(outgoing.operation)) {
          if (currentGesture && !currentGesture.used) {
            currentGesture.used = true;
            outgoing.gesture_id = currentGesture.id;
          }
        }
        relay(outgoing);
      };
      Object.defineProperty(window, "__DOT_APP_POST__", {
        configurable: false,
        enumerable: false,
        writable: false,
        value: receiveGuestMessage,
      });

      const startApp = () => {
        if (appStarted) return;
        appStarted = true;
        const script = document.createElement("script");
        script.textContent = bundleCode;
        script.addEventListener("error", () => reportError("app script failed to start"));
        document.body.appendChild(script);
      };
      window.addEventListener("message", (event) => {
        if (
          event.source !== window.parent ||
          !event.data ||
          event.data.type !== "dot.host.port" ||
          event.data.channel_token !== channelToken ||
          event.ports.length !== 1 ||
          hostPort
        ) return;
        hostPort = event.ports[0];
        hostPort.onmessage = (portEvent) => {
          const message = portEvent.data;
          if (!message || typeof message !== "object" || message.channel_token !== channelToken) return;
          window.dispatchEvent(new MessageEvent("message", {
            data: message,
            source: window.parent,
          }));
        };
        hostPort.start();
        while (queuedMessages.length) call(portPostMessage, hostPort, [queuedMessages.shift()]);
        defer(startApp);
      });
      window.addEventListener("error", (event) => reportError(event.error || event.message));
      window.addEventListener("unhandledrejection", (event) => reportError(event.reason));
      Object.defineProperty(window, "__DOT_APP_CHANNEL_TOKEN__", {
        configurable: false,
        enumerable: false,
        writable: false,
        value: channelToken,
      });
      window.__DOT_APP_CONTEXT__ = { data: ${scriptLiteral(context)} };
    })();
  `;
  return `<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<style>${styleText(bundle.css)}</style></head>
<body><div id="dot-app-root"></div><script>${bootstrap.replaceAll("</script", "<\\/script")}</script></body></html>`;
}

function hostDocument(frameDocument, context, channelToken) {
  const source = `
    (() => {
      const token = ${scriptLiteral(channelToken)};
      const initialContext = ${scriptLiteral(context)};
      const frameDocument = ${scriptLiteral(frameDocument)};
      const state = {
        ready: false,
        operations: [],
        runtimeErrors: [],
        violations: [],
        records: {},
        nextId: 1,
      };
      Object.defineProperty(window, "__DOT_ACCEPTANCE__", {
        configurable: false,
        enumerable: false,
        writable: false,
        value: state,
      });
      const usedGestures = new Set();
      const mutationNames = new Set([
        "records.create", "records.update", "records.delete", "dot.reminder.create"
      ]);
      const clone = (value) => JSON.parse(JSON.stringify(value));
      const frame = document.createElement("iframe");
      frame.name = "dot-app";
      frame.title = "Dot generated app acceptance";
      frame.setAttribute("sandbox", "allow-scripts allow-forms");
      frame.setAttribute("referrerpolicy", "no-referrer");
      frame.style.cssText = "display:block;width:390px;height:844px;border:0";
      frame.addEventListener("load", () => {
        const channel = new MessageChannel();
        const port = channel.port1;
        const respond = (requestId, payload) => port.postMessage({
          type: "dot.app.response",
          protocol_version: 1,
          sdk_version: "2",
          channel_token: token,
          request_id: requestId,
          ...payload,
        });
        port.onmessage = (event) => {
          const message = event.data;
          if (!message || typeof message !== "object" || message.channel_token !== token) return;
          if (message.type === "dot.app.ready") {
            state.ready = true;
            return;
          }
          if (message.type === "dot.app.error") {
            state.runtimeErrors.push(clone(message.error || { message: "app runtime failed" }));
            return;
          }
          if (message.type !== "dot.app.request" || typeof message.request_id !== "string") return;
          const operation = String(message.operation || "");
          const args = message.args && typeof message.args === "object" ? clone(message.args) : {};
          const mutating = mutationNames.has(operation);
          if (
            operation === "records.list" &&
            (
              !Number.isInteger(args.limit) || args.limit < 1 || args.limit > 100 ||
              !Number.isInteger(args.offset) || args.offset < 0
            )
          ) {
            respond(message.request_id, {
              ok: false,
              error: { code: "invalid_arguments", message: "Invalid record list page" },
            });
            return;
          }
          if (mutating && (!message.gesture_id || usedGestures.has(message.gesture_id))) {
            state.violations.push({ code: "background_mutation", operation });
            respond(message.request_id, {
              ok: false,
              error: { code: "user_gesture_required", message: "A real user gesture is required" },
            });
            return;
          }
          if (mutating) usedGestures.add(message.gesture_id);
          const sequence = state.operations.length + 1;
          state.operations.push({ operation, args, sequence, mutating });
          if (state.operations.length > ${MAX_OPERATIONS}) {
            state.violations.push({ code: "real_browser_operation_limit", operation });
            respond(message.request_id, {
              ok: false,
              error: { code: "rate_limited", message: "Too many app operations" },
            });
            return;
          }
          let result = {};
          if (operation === "app.data.get") result = clone(initialContext);
          else if (operation === "records.list") {
            const records = state.records[String(args.entity)] || [];
            result = { data: clone(records), meta: { total: records.length } };
          } else if (operation === "records.create") {
            const entity = String(args.entity || "");
            const now = "2026-01-01T09:00:00Z";
            const suffix = String(state.nextId++).padStart(12, "0");
            const record = {
              ...(args.data && typeof args.data === "object" ? clone(args.data) : {}),
              id: "00000000-0000-4000-8000-" + suffix,
              entity,
              version: 1,
              created_at: now,
              updated_at: now,
            };
            state.records[entity] ||= [];
            state.records[entity].push(record);
            result = clone(record);
          } else if (operation === "dot.reminder.create") {
            result = { id: "acceptance-reminder", status: "scheduled" };
          }
          queueMicrotask(() => respond(message.request_id, { ok: true, result }));
        };
        port.start();
        frame.contentWindow.postMessage({ type: "dot.host.port", channel_token: token }, "*", [channel.port2]);
        port.postMessage({
          type: "dot.app.context",
          protocol_version: 1,
          sdk_version: "2",
          channel_token: token,
          data: initialContext,
        });
      });
      frame.srcdoc = frameDocument;
      document.body.appendChild(frame);
    })();
  `;
  return `<!doctype html><html><head><meta charset="utf-8"></head><body><script>${source.replaceAll("</script", "<\\/script")}</script></body></html>`;
}

function selectorField(formSelector, name) {
  const safeName = String(name).replace(/[^a-zA-Z0-9_-]/g, "");
  return `${formSelector} [name="${safeName}"]`;
}

async function visible(locator) {
  return await locator.count() > 0 && await locator.first().isVisible();
}

async function expectSingleVisible(frame, selector, { required, label }) {
  const locator = frame.locator(selector);
  const count = await locator.count();
  if (count === 0 || !(await locator.first().isVisible())) {
    if (required) fail("acceptance_flow_missing", `${label} was not rendered and visible: ${selector}`);
    return null;
  }
  if (count !== 1) fail("acceptance_flow_ambiguous", `${label} matched ${count} elements: ${selector}`);
  return locator.first();
}

async function assertFocused(locator, selector) {
  const focused = await locator.evaluate((element) => element === element.ownerDocument.activeElement);
  if (!focused) fail("acceptance_input_focus_lost", `field lost focus while typing: ${selector}`);
}

async function enterField(frame, selector, value) {
  const locator = await expectSingleVisible(frame, selector, {
    required: true,
    label: "required field",
  });
  if (!(await locator.isEnabled())) fail("acceptance_flow_missing", `required field is disabled: ${selector}`);
  const metadata = await locator.evaluate((element) => ({
    tag: element.tagName.toLowerCase(),
    type: element instanceof HTMLInputElement ? element.type.toLowerCase() : "",
  }));
  if (metadata.tag === "select") {
    await locator.focus();
    await assertFocused(locator, selector);
    const firstEnabled = await locator.locator("option:not(:disabled)").evaluateAll((options) => {
      const preferred = options.find((option) => option.value !== "") || options[0];
      return preferred ? preferred.value : null;
    });
    if (firstEnabled === null) {
      fail("acceptance_flow_missing", `select has no enabled option: ${selector}`);
    }
    await locator.selectOption(firstEnabled);
    return { mode: "select", value: await locator.inputValue() };
  }
  if (metadata.type === "checkbox" || metadata.type === "radio") {
    const desired = Boolean(value);
    if ((await locator.isChecked()) !== desired) await locator.click();
    if ((await locator.isChecked()) !== desired) {
      fail("acceptance_field_value_mismatch", `field did not preserve acceptance value: ${selector}`);
    }
    return { mode: "click", value: desired };
  }
  const desired = typeof value === "object" ? JSON.stringify(value) : String(value);
  await locator.click();
  await assertFocused(locator, selector);
  if (["date", "datetime-local", "month", "time", "week"].includes(metadata.type)) {
    await locator.fill(desired.replace(/Z$/, ""));
    await assertFocused(locator, selector);
    return { mode: "browser-fill", value: await locator.inputValue() };
  }
  await locator.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
  if ((await locator.inputValue()).length) await locator.press("Backspace");
  let typed = "";
  for (const character of desired) {
    await assertFocused(locator, selector);
    await frame.page().keyboard.type(character);
    typed += character;
    await assertFocused(locator, selector);
    const current = await locator.inputValue();
    if (current !== typed) {
      fail(
        "acceptance_field_value_mismatch",
        `field did not preserve the typed acceptance value after ${typed.length} character(s): ${selector}`,
      );
    }
  }
  return { mode: "trusted-keyboard", value: await locator.inputValue(), characters: desired.length };
}

async function hostState(page) {
  return page.evaluate(() => JSON.parse(JSON.stringify(window.__DOT_ACCEPTANCE__)));
}

function mutationMatches(operation, step) {
  if (operation.operation !== step.operation) return false;
  if (typeof step.entity === "string" && operation.args?.entity !== step.entity) return false;
  const data = operation.args?.data;
  const requiredFields = Array.isArray(step.required_payload_fields)
    ? step.required_payload_fields
    : step.required_fields;
  if (Array.isArray(requiredFields)) {
    if (!data || typeof data !== "object") return false;
    if (requiredFields.some((field) => data[field] === undefined || data[field] === null)) {
      return false;
    }
  }
  const allowedFields = Array.isArray(step.allowed_payload_fields)
    ? step.allowed_payload_fields
    : step.allowed_fields;
  if (Array.isArray(allowedFields) && data && typeof data === "object") {
    if (Object.keys(data).some((field) => !allowedFields.includes(field))) return false;
  }
  return true;
}

async function runAcceptance(page, frame, acceptancePlan, timeoutMs) {
  const fieldTyping = [];
  const acceptance = [];
  let requiredMutationCount = 0;
  let refreshVerified = 0;
  let persistedRenderVerified = 0;
  for (const step of acceptancePlan) {
    if (!step || typeof step !== "object" || typeof step.selector !== "string") continue;
    const required = step.required === true;
    if (step.operation === "ui.reveal_primary") {
      const trigger = await expectSingleVisible(frame, step.selector, {
        required,
        label: "primary workflow trigger",
      });
      if (trigger) await trigger.click();
      acceptance.push({ operation: step.operation, passed: true });
      continue;
    }
    const target = await expectSingleVisible(frame, step.selector, {
      required,
      label: "interaction target",
    });
    if (!target) {
      acceptance.push({ operation: step.operation, entity: step.entity, passed: !required });
      continue;
    }
    const fieldHints = step.field_hints && typeof step.field_hints === "object"
      ? step.field_hints
      : step.fields && typeof step.fields === "object"
        ? step.fields
        : {};
    for (const [name, value] of Object.entries(fieldHints)) {
      const fieldSelector = selectorField(step.selector, name);
      if (!(await visible(frame.locator(fieldSelector)))) {
        // Field names are only interaction hints. Purpose-built controls may derive a persisted
        // field from a segmented control, stepper, or several differently named inputs. The
        // submitted mutation payload below is the authoritative data-contract check.
        continue;
      }
      fieldTyping.push({ field: name, ...(await enterField(frame, fieldSelector, value)) });
    }
    if (step.event_type !== "submit") {
      await target.click();
      acceptance.push({ operation: step.operation, entity: step.entity, passed: true });
      continue;
    }
    const submitSelector = 'button[type="submit"]';
    const submitCandidates = target.locator(submitSelector);
    const submitCount = await submitCandidates.count();
    if (submitCount === 0 || !(await submitCandidates.first().isVisible())) {
      if (required) {
        fail(
          "acceptance_flow_missing",
          `visible submit button was not rendered inside target form: ${step.selector}`,
        );
      }
      continue;
    }
    if (submitCount !== 1) {
      fail(
        "acceptance_flow_ambiguous",
        `target form contained ${submitCount} submit buttons: ${step.selector}`,
      );
    }
    const submit = submitCandidates.first();
    const submitMetadata = await submit.evaluate((element) => ({
      type: element instanceof HTMLButtonElement ? element.type : "",
      form: element instanceof HTMLButtonElement ? Boolean(element.form) : false,
      disabled: element instanceof HTMLButtonElement ? element.disabled : false,
    }));
    const before = await hostState(page);
    const beforeText = (await frame.locator("body").innerText()).trim();
    const beforeMutations = before.operations.filter((item) => item.mutating).length;
    const beforeLists = before.operations.filter(
      (item) => item.operation === "records.list" && item.args?.entity === step.entity,
    ).length;
    await submit.click();
    await page.waitForFunction(
      (minimum) => window.__DOT_ACCEPTANCE__.operations.filter((item) => item.mutating).length >= minimum,
      beforeMutations + 1,
      { timeout: Math.min(timeoutMs, 4_000) },
    ).catch(async () => {
      const debugState = await hostState(page).catch(() => ({}));
      const body = await frame.locator("body").innerText().catch(() => "");
      fail(
        "acceptance_flow_missing",
        `submit did not call ${step.operation}; submit=${JSON.stringify(submitMetadata)} state=${JSON.stringify(debugState)} body=${body.slice(0, 300)}`,
      );
    });
    await page.waitForTimeout(200);
    const after = await hostState(page);
    if (after.violations.length) {
      fail(after.violations[0].code, "one user gesture attempted more than one mutation");
    }
    const newMutations = after.operations.filter((item) => item.mutating).slice(beforeMutations);
    if (newMutations.length !== 1) {
      fail(
        "acceptance_duplicate_mutation",
        `one submit produced ${newMutations.length} mutations; expected exactly one ${step.operation}`,
      );
    }
    if (!mutationMatches(newMutations[0], step)) {
      fail(
        "acceptance_required_field_missing",
        `${step.operation} did not persist every required declared field`,
      );
    }
    requiredMutationCount += required ? 1 : 0;
    if (step.operation === "records.create" && typeof step.entity === "string") {
      await page.waitForFunction(
        ([entity, minimum]) => window.__DOT_ACCEPTANCE__.operations.filter(
          (item) => item.operation === "records.list" && item.args?.entity === entity,
        ).length > minimum,
        [step.entity, beforeLists],
        { timeout: Math.min(timeoutMs, 4_000) },
      ).catch(() => fail(
        "acceptance_records_refresh_missing",
        `${step.entity} was saved but the app did not refresh its records`,
      ));
      refreshVerified += 1;
      let afterText = beforeText;
      await frame.waitForFunction(
        (previous) => document.body.innerText.trim() !== previous,
        beforeText,
        { timeout: 600 },
      ).catch(() => undefined);
      afterText = (await frame.locator("body").innerText()).trim();
      if (afterText !== beforeText) persistedRenderVerified += 1;
      else if (required) {
        fail(
          "acceptance_persisted_result_missing",
          `${step.entity} refreshed after save but the persisted result was not visible`,
        );
      }
    }
    acceptance.push({ operation: step.operation, entity: step.entity, passed: true });
  }
  return {
    acceptance,
    field_typing: fieldTyping,
    required_mutations_verified: requiredMutationCount,
    record_refreshes_verified: refreshVerified,
    persisted_renders_verified: persistedRenderVerified,
  };
}

async function assertChromiumSandbox(browser, browserPath, timeoutMs) {
  if (process.env.DOT_APP_CHROMIUM_REQUIRE_SANDBOX !== "true" || process.platform !== "linux") {
    return;
  }
  if (!String(browserPath).toLowerCase().includes("chrom")) {
    fail("real_browser_unavailable", "sandbox verification requires Chromium on Linux");
  }
  const page = await browser.newPage();
  try {
    await page.goto("chrome://sandbox", { waitUntil: "domcontentloaded", timeout: timeoutMs });
    const status = await page.locator("body").innerText();
    if (!/adequately sandboxed/i.test(status)) {
      fail("real_browser_unavailable", "Chromium reported that its process sandbox is not active");
    }
  } finally {
    await page.close().catch(() => undefined);
  }
}

let browser;
try {
  const request = await input();
  const browserPath = executablePath();
  if (!browserPath) fail("real_browser_unavailable", "Chromium executable was not found");
  const timeoutMs = Math.min(Math.max(Number(request.timeout_ms) || 12_000, 2_000), 20_000);
  // Missing or malformed values fail closed. The worker emits exactly "false" only after
  // validating the explicit Railway-only fallback.
  const nativeSandboxRequired = process.env.DOT_APP_CHROMIUM_REQUIRE_SANDBOX !== "false";
  const channelToken = `dot_acceptance_${crypto.randomUUID().replaceAll("-", "_")}`;
  const context = request.context && typeof request.context === "object" ? request.context : {};
  browser = await chromium.launch({
    executablePath: browserPath,
    headless: true,
    // Generated code is untrusted even after the static and QuickJS gates. Native sandboxing is
    // the fail-closed default. The worker may explicitly disable it only through the audited
    // Railway fallback; request interception remains a behavioral assertion, not process or
    // container-level isolation.
    chromiumSandbox: nativeSandboxRequired,
    timeout: Math.min(timeoutMs, 8_000),
    args: [
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-extensions",
      "--disable-sync",
      "--metrics-recording-only",
      "--no-first-run",
      "--renderer-process-limit=1",
      "--js-flags=--max-old-space-size=128",
      ...(nativeSandboxRequired ? [] : ["--no-sandbox"]),
    ],
  });
  await assertChromiumSandbox(browser, browserPath, Math.min(timeoutMs, 4_000));
  const contextHandle = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 1,
    javaScriptEnabled: true,
    serviceWorkers: "block",
    acceptDownloads: false,
  });
  let networkAttempts = 0;
  await contextHandle.route("**/*", async (route) => {
    networkAttempts += 1;
    await route.abort("blockedbyclient");
  });
  const page = await contextHandle.newPage();
  page.setDefaultTimeout(Math.min(timeoutMs, 4_000));
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error?.message || error).slice(0, 800)));
  const inner = iframeDocument(request.bundle, context, channelToken);
  await page.setContent(hostDocument(inner, context, channelToken), { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.__DOT_ACCEPTANCE__?.ready === true, undefined, {
    timeout: timeoutMs,
  }).catch(async () => {
    const state = await hostState(page).catch(() => ({}));
    const frames = page.frames().map((item) => ({ name: item.name(), url: item.url() }));
    fail(
      "real_browser_not_ready",
      `app did not report ready in real Chromium; state=${JSON.stringify(state)} frames=${JSON.stringify(frames)} errors=${JSON.stringify(pageErrors)}`,
    );
  });
  const frame = page.frames().find((item) => item !== page.mainFrame());
  if (!frame) fail("real_browser_not_ready", "generated app frame was not created");
  const acceptancePlan = Array.isArray(request.acceptance_plan)
    ? request.acceptance_plan.slice(0, MAX_STEPS)
    : [];
  const acceptanceResult = await runAcceptance(page, frame, acceptancePlan, timeoutMs);
  const state = await hostState(page);
  const runtimeErrors = [...pageErrors, ...state.runtimeErrors.map((item) => item.message || item.name)];
  if (runtimeErrors.length) fail("browser_runtime_error", runtimeErrors[0]);
  if (state.violations.length) fail(state.violations[0].code, "app attempted an untrusted mutation");
  if (networkAttempts) fail("network_access", "generated app attempted network access in Chromium");
  respond({
    ok: true,
    result: {
      ready: true,
      rendered: (await frame.locator("body").innerText()).trim().length > 0,
      runtime: "chromium",
      process_sandbox: nativeSandboxRequired
        ? "native"
        : "disabled-explicit-railway-fallback",
      runtime_errors: 0,
      network_attempts: networkAttempts,
      operations: state.operations,
      ...acceptanceResult,
    },
  });
} catch (error) {
  const message = String(error?.message || error);
  const unavailable = /browserType\.launch|Failed to move to new namespace|chrome-sandbox/i.test(message);
  const timeout = /Timeout|timed out/i.test(message);
  respond({
    ok: false,
    issues: [issue(
      unavailable
        ? "real_browser_unavailable"
        : timeout
          ? "real_browser_timeout"
          : (error?.code || "real_browser_runtime_error"),
      message,
    )],
  }, 1);
} finally {
  try { await browser?.close(); } catch {}
}
