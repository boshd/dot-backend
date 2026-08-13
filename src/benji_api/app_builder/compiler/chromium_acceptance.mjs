import fs from "node:fs";
import { chromium } from "playwright-core";

const MAX_INPUT_BYTES = 3_600_000;
const MAX_OUTPUT_BYTES = 512_000;
const MAX_STEPS = 24;
const MAX_OPERATIONS = 100;
const MAX_REVEAL_CLICKS = 8;
let acceptanceTargetSequence = 0;

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

function hostDocument(frameDocument, context, channelToken, initialRecords) {
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
        records: ${scriptLiteral(initialRecords && typeof initialRecords === "object" ? initialRecords : {})},
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
          } else if (operation === "records.update") {
            const recordId = String(args.record_id || "");
            const patch = args.data && typeof args.data === "object" ? clone(args.data) : {};
            let updated = null;
            for (const entity of Object.keys(state.records)) {
              const list = state.records[entity] || [];
              const index = list.findIndex((item) => item.id === recordId);
              if (index < 0) continue;
              const current = list[index];
              if (args.expected_version != null && current.version !== args.expected_version) break;
              const merged = { ...current };
              for (const [key, value] of Object.entries(patch)) {
                if (value === null) delete merged[key];
                else merged[key] = value;
              }
              merged.version = current.version + 1;
              merged.updated_at = "2026-01-01T09:00:00Z";
              list[index] = merged;
              updated = clone(merged);
              break;
            }
            result = updated || {};
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

function stepFieldHints(step) {
  return step.field_hints && typeof step.field_hints === "object"
    ? step.field_hints
    : step.fields && typeof step.fields === "object"
      ? step.fields
      : {};
}

function workflowIdentity(step) {
  return {
    operation: String(step.operation || ""),
    entity: typeof step.entity === "string" ? step.entity : "",
  };
}

function workflowIdentityText(identity) {
  return `operation=${identity.operation || "(missing)"} entity=${identity.entity || "(none)"}`;
}

function observedMutationIdentity(operation) {
  return {
    operation: String(operation?.operation || ""),
    entity: String(operation?.args?.entity || ""),
  };
}

async function discoverSubmitForm(frame, step) {
  const forms = frame.locator("form");
  const candidates = await forms.evaluateAll((elements, rawStep) => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" &&
        Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const fieldNames = Object.keys(
      rawStep.field_hints && typeof rawStep.field_hints === "object"
        ? rawStep.field_hints
        : rawStep.fields && typeof rawStep.fields === "object"
          ? rawStep.fields
          : {},
    );
    const requiredNames = Array.isArray(rawStep.required_payload_fields)
      ? rawStep.required_payload_fields
      : Array.isArray(rawStep.required_fields)
        ? rawStep.required_fields
        : [];
    return elements.map((form, index) => {
      const markedOperation = String(form.getAttribute("data-dot-operation") || "");
      const markedEntity = String(form.getAttribute("data-dot-entity") || "");
      const semantic = Boolean(markedOperation || markedEntity);
      const expectedOperation = String(rawStep.operation || "");
      const expectedEntity = typeof rawStep.entity === "string" ? rawStep.entity : "";
      const exactSemantic = semantic && markedOperation === expectedOperation &&
        (!expectedEntity || markedEntity === expectedEntity);
      const submitters = [...form.querySelectorAll('button[type="submit"], input[type="submit"]')]
        .filter(visible);
      const present = fieldNames.filter((name) => {
        const safe = CSS.escape(String(name));
        return [...form.querySelectorAll(`[name="${safe}"]`)].some(visible);
      });
      const required = requiredNames.filter((name) => present.includes(name));
      const submitLabels = submitters.map((element) => [
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
        element.textContent,
      ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().slice(0, 100));
      return {
        index,
        semantic,
        exactSemantic,
        markedOperation,
        markedEntity,
        visible: visible(form),
        submitters: submitters.length,
        submitLabels,
        present,
        missingRequired: requiredNames.filter((name) => !required.includes(name)),
        score: required.length * 100 + present.length,
      };
    });
  }, step);
  const exactVisible = candidates.filter((item) => item.exactSemantic && item.visible);
  if (exactVisible.length > 1) {
    fail(
      "acceptance_flow_ambiguous",
      `multiple visible workflows declare ${workflowIdentityText(workflowIdentity(step))}; candidates=${JSON.stringify(candidates)}`,
    );
  }
  if (exactVisible.length === 1) {
    const exact = exactVisible[0];
    if (exact.submitters !== 1) {
      fail(
        exact.submitters > 1 ? "acceptance_flow_ambiguous" : "acceptance_flow_missing",
        `workflow declaring ${workflowIdentityText(workflowIdentity(step))} has ${exact.submitters} visible submit controls; expected exactly one`,
      );
    }
    return {
      form: forms.nth(exact.index),
      candidates,
      score: exact.score,
      matchKind: "semantic",
      exactSemanticPresent: true,
    };
  }

  // Marked forms belong to their declared workflow. Never reinterpret a different marked
  // entity merely because it happens to share field names with the expected workflow.
  const usable = candidates.filter(
    (item) => !item.semantic && item.visible && item.submitters === 1,
  );
  if (!usable.length) {
    return {
      form: null,
      candidates,
      score: 0,
      matchKind: null,
      exactSemanticPresent: candidates.some((item) => item.exactSemantic),
    };
  }
  const bestScore = Math.max(...usable.map((item) => item.score));
  const best = usable.filter((item) => item.score === bestScore);
  if (best.length !== 1) {
    const reason = bestScore === 0 ? "with no declared-field signal" : `at score ${bestScore}`;
    fail(
      "acceptance_flow_ambiguous",
      `could not identify one ${step.entity || step.operation} form; ${best.length} visible forms tied ${reason}; candidates=${JSON.stringify(candidates)}`,
    );
  }
  return {
    form: forms.nth(best[0].index),
    candidates,
    score: bestScore,
    matchKind: "legacy",
    exactSemanticPresent: candidates.some((item) => item.exactSemantic),
  };
}

function flowDiagnostics(step, discovery, revealState) {
  const required = Array.isArray(step.required_payload_fields)
    ? step.required_payload_fields
    : Array.isArray(step.required_fields)
      ? step.required_fields
      : [];
  const forms = (discovery?.candidates || []).map((item) => ({
    index: item.index,
    marker: item.semantic ? {
      operation: item.markedOperation || null,
      entity: item.markedEntity || null,
      exact: item.exactSemantic,
    } : null,
    visible: item.visible,
    submit: item.submitLabels,
    present: item.present,
    missing_required: item.missingRequired,
  }));
  const explored = (revealState?.explored || []).map((item) => item.label);
  return `expected=${JSON.stringify(workflowIdentity(step))} required=${JSON.stringify(required)} forms=${JSON.stringify(forms)} explored=${JSON.stringify(explored)}`;
}

async function revealSemanticForm(page, frame, step, revealState, { exactOnly = false } = {}) {
  let latestDiscovery = revealState.discovery;
  while (revealState.clicks < MAX_REVEAL_CLICKS) {
    const prefix = `dot-acceptance-reveal-${++acceptanceTargetSequence}`;
    const candidates = await frame.evaluate(({ rawStep, seen, markerPrefix, requireExact }) => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
      };
      const entity = String(rawStep.entity || "").replaceAll("_", " ").toLowerCase();
      const expectedOperation = String(rawStep.operation || "");
      const expectedEntity = typeof rawStep.entity === "string" ? rawStep.entity : "";
      const entityParts = entity.split(/\s+/).filter((part) => part.length > 2);
      const occurrences = new Map();
      const result = [];
      for (const [index, element] of [...document.querySelectorAll(
        "button, [role=button], [role=tab], summary",
      )].entries()) {
        if (!visible(element) || element.closest("form")) continue;
        if (element.hasAttribute("disabled") || element.getAttribute("aria-disabled") === "true") {
          continue;
        }
        const explicitType = String(element.getAttribute("type") || "").toLowerCase();
        if (["submit", "reset"].includes(explicitType)) continue;
        const markedOperation = String(element.getAttribute("data-dot-operation") || "");
        const markedEntity = String(element.getAttribute("data-dot-entity") || "");
        const semantic = Boolean(markedOperation || markedEntity);
        const exactSemantic = semantic && markedOperation === expectedOperation &&
          (!expectedEntity || markedEntity === expectedEntity);
        if ((requireExact && !exactSemantic) || (!requireExact && semantic && !exactSemantic)) {
          continue;
        }
        const label = [
          element.getAttribute("aria-label"),
          element.getAttribute("title"),
          element.textContent,
        ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().slice(0, 160);
        const normalized = label.toLowerCase();
        if (!normalized || /\b(?:delete|remove|disconnect|sign out|log out|reset|clear|destroy)\b/.test(normalized)) {
          continue;
        }
        const baseKey = [
          element.tagName.toLowerCase(),
          element.getAttribute("role") || "",
          normalized,
        ].join("|");
        const occurrence = occurrences.get(baseKey) || 0;
        occurrences.set(baseKey, occurrence + 1);
        const key = `${baseKey}|${occurrence}`;
        if (seen.includes(key)) continue;
        let score = 0;
        if (exactSemantic) score += 10_000;
        if (entity && normalized.includes(entity)) score += 120;
        score += entityParts.filter((part) => normalized.includes(part)).length * 30;
        if (/\b(?:add|new|create|log|start|open|show|edit|manage)\b/.test(normalized)) score += 40;
        if (element.hasAttribute("data-dot-primary-action")) score += 25;
        if (element.getAttribute("role") === "tab") score += 15;
        if (element.getAttribute("aria-expanded") === "false") score += 10;
        const marker = `${markerPrefix}-${index}`;
        element.setAttribute("data-dot-acceptance-reveal", marker);
        result.push({
          key,
          label,
          score,
          index,
          semantic,
          exactSemantic,
          markedOperation,
          markedEntity,
          selector: `[data-dot-acceptance-reveal="${marker}"]`,
        });
      }
      return result.sort((left, right) => right.score - left.score || left.index - right.index);
    }, {
      rawStep: step,
      seen: [...revealState.seen],
      markerPrefix: prefix,
      requireExact: exactOnly,
    });
    const candidate = candidates[0];
    if (!candidate) {
      revealState.discovery = latestDiscovery;
      return null;
    }
    revealState.seen.add(candidate.key);
    revealState.clicks += 1;
    if (candidate.exactSemantic) revealState.exactTargetAttempted = true;
    const locator = frame.locator(candidate.selector);
    if (await locator.count() !== 1 || !(await locator.isVisible()) || !(await locator.isEnabled())) {
      continue;
    }
    const before = await hostState(page);
    const beforeText = (await frame.locator("body").innerText()).trim();
    const beforeMutations = before.operations.filter((item) => item.mutating).length;
    const beforeLists = before.operations.filter(
      (item) => item.operation === "records.list" && item.args?.entity === step.entity,
    ).length;
    const beforeViolations = before.violations.length;
    try {
      await locator.click({ timeout: 1_000 });
    } catch {
      continue;
    }
    await page.waitForTimeout(150);
    const after = await hostState(page);
    const newMutations = after.operations.filter((item) => item.mutating).slice(beforeMutations);
    const newViolations = after.violations.slice(beforeViolations);
    if (newViolations.length) {
      fail(
        "acceptance_reveal_mutation",
        `workflow action “${candidate.label}” violated the gesture contract: ${JSON.stringify(newViolations)}`,
      );
    }
    if (newMutations.length) {
      if (newMutations.length !== 1) {
        fail(
          "acceptance_duplicate_mutation",
          `workflow action “${candidate.label}” produced ${newMutations.length} mutations; expected exactly one ${step.operation}`,
        );
      }
      if (!mutationIdentityMatches(newMutations[0], step)) {
        const expected = workflowIdentity(step);
        const observed = observedMutationIdentity(newMutations[0]);
        fail(
          "acceptance_workflow_mismatch",
          `workflow action “${candidate.label}” declared ${workflowIdentityText({ operation: candidate.markedOperation, entity: candidate.markedEntity })} but performed the wrong mutation; expected ${workflowIdentityText(expected)}, observed ${workflowIdentityText(observed)}`,
        );
      }
      if (!mutationPayloadMatches(newMutations[0], step)) {
        fail(
          "acceptance_required_field_missing",
          `workflow action “${candidate.label}” produced an invalid payload for ${workflowIdentityText(workflowIdentity(step))}; ${payloadDiagnostics(newMutations[0], step)}`,
        );
      }
      revealState.explored.push({
        entity: step.entity,
        label: candidate.label,
        matched: true,
        direct_action: true,
        semantic: candidate.exactSemantic || undefined,
      });
      return {
        kind: "direct_action",
        beforeMutations,
        beforeLists,
        beforeText,
      };
    }
    await assertVisibleExperience(frame);
    const previouslyHadForm = Boolean(latestDiscovery?.form);
    const discovery = await discoverSubmitForm(frame, step);
    latestDiscovery = discovery;
    revealState.explored.push({
      entity: step.entity,
      label: candidate.label,
      matched: Boolean(discovery.form),
      semantic: candidate.exactSemantic || undefined,
    });
    if (discovery.form && (
      discovery.matchKind === "semantic" || discovery.score > 0 || !previouslyHadForm
    )) {
      return { kind: "form", target: discovery.form };
    }
  }
  revealState.discovery = latestDiscovery;
  return null;
}

async function interactionTarget(page, frame, step, { required, revealState }) {
  if (step.event_type === "submit") {
    const discovery = await discoverSubmitForm(frame, step);
    revealState.discovery = discovery;
    if (discovery.form && discovery.matchKind === "semantic") {
      return { kind: "form", target: discovery.form };
    }

    // A targeted SDK trigger is stronger evidence than a currently visible legacy form. This
    // matters when two workflows share fields such as `name`: open the exact declared workflow
    // rather than submitting whichever unrelated form happens to be visible.
    const exactReveal = await revealSemanticForm(
      page,
      frame,
      step,
      revealState,
      { exactOnly: true },
    );
    if (exactReveal) return exactReveal;

    if (
      discovery.form && discovery.score > 0 &&
      !discovery.exactSemanticPresent && !revealState.exactTargetAttempted
    ) {
      return { kind: "form", target: discovery.form };
    }
    if (discovery.exactSemanticPresent || revealState.exactTargetAttempted) {
      if (required) {
        fail(
          "acceptance_flow_missing",
          `the exact declared workflow could not be opened; ${flowDiagnostics(step, revealState.discovery, revealState)}`,
        );
      }
      return null;
    }
    const revealed = await revealSemanticForm(page, frame, step, revealState);
    if (revealed) return revealed;
    // A single zero-signal form may still implement a valid derived-data workflow. Its observed
    // mutation remains authoritative, but multiple zero-signal forms are rejected above.
    const zeroSignalDiscovery = revealState.discovery || discovery;
    if (zeroSignalDiscovery.form && !zeroSignalDiscovery.exactSemanticPresent) {
      return { kind: "form", target: zeroSignalDiscovery.form };
    }
    if (required) {
      fail(
        "acceptance_flow_missing",
        `no usable ${step.entity || step.operation} create workflow was found; ${flowDiagnostics(step, revealState.discovery, revealState)}`,
      );
    }
    return null;
  }
  if (typeof step.selector !== "string") {
    if (required) fail("acceptance_flow_missing", `no interaction target declared for ${step.operation}`);
    return null;
  }
  const target = await expectSingleVisible(frame, step.selector, {
    required,
    label: "interaction target",
  });
  return target ? { kind: "target", target } : null;
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

async function auditVisibleExperience(frame) {
  return frame.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = document.documentElement.clientHeight;
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" &&
        Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const clipped = (rect) => {
      const pageTop = rect.top + window.scrollY;
      const pageBottom = rect.bottom + window.scrollY;
      const documentHeight = Math.max(
        viewportHeight,
        document.documentElement.scrollHeight,
        document.body.scrollHeight,
      );
      return rect.left < -1 || rect.right > viewportWidth + 1 ||
        pageTop < -1 || pageBottom > documentHeight + 1;
    };
    const textOf = (element) => String(element.innerText || element.textContent || "")
      .replace(/\s+/g, " ").trim();
    const describe = (element) => {
      const text = textOf(element).slice(0, 80);
      const name = element.getAttribute("name");
      return `${element.tagName.toLowerCase()}${name ? `[name=${name}]` : ""}${text ? ` “${text}”` : ""}`;
    };
    const issues = [];
    const push = (code, message) => {
      if (issues.length >= 20) return;
      if (!issues.some((item) => item.code === code && item.message === message)) {
        issues.push({ code, message: String(message).slice(0, 800) });
      }
    };

    if (document.documentElement.scrollWidth > viewportWidth + 1 ||
        document.body.scrollWidth > viewportWidth + 1) {
      push(
        "ux_horizontal_overflow",
        `app content is ${Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)}px wide in a ${viewportWidth}px mobile viewport`,
      );
    }

    const headings = [...document.querySelectorAll("h1")].filter(visible);
    if (headings.length !== 1) {
      push("ux_heading_hierarchy", `app must render exactly one visible h1; found ${headings.length}`);
    } else {
      const heading = headings[0];
      const rect = heading.getBoundingClientRect();
      const fontSize = Number.parseFloat(getComputedStyle(heading).fontSize);
      const lineHeight = Number.parseFloat(getComputedStyle(heading).lineHeight) || fontSize;
      const lineCount = Math.max(1, Math.round(rect.height / lineHeight));
      const shell = document.querySelector(".dot-app-shell");
      const comfortable = shell?.getAttribute("data-density") === "comfortable";
      const maxHeading = shell && !comfortable ? 24 : 56;
      if (fontSize > maxHeading || rect.height > viewportHeight * 0.35 || lineCount > 3) {
        push(
          "ux_giant_heading",
          `h1 is too dominant on mobile (${Math.round(fontSize)}px, ${lineCount} lines, ${Math.round(rect.height)}px tall)`,
        );
      }
    }

    if (document.querySelector("label label")) {
      push("ux_nested_label", "nested labels are invalid HTML and break checkbox/field clicks");
    }

    for (const leading of document.querySelectorAll(".dot-list-leading")) {
      if (!visible(leading)) continue;
      if (leading.scrollWidth > leading.clientWidth + 1 || leading.scrollHeight > leading.clientHeight + 1) {
        push(
          "ux_leading_overflow",
          `list leading overflows its 44px slot (${leading.scrollWidth}x${leading.scrollHeight} in ${leading.clientWidth}x${leading.clientHeight})`,
        );
      }
    }
    for (const meta of document.querySelectorAll(".dot-list-meta")) {
      if (getComputedStyle(meta).display === "none") {
        push("ux_hidden_list_meta", "list item meta is hidden on the mobile viewport");
      }
    }

    const overlapSkip = (element) =>
      element.classList.contains("dot-sr-only") || element.closest(".dot-list-leading");
    const textNodes = [...document.querySelectorAll("h1,h2,h3,p,span,strong,small,label")]
      .filter(visible)
      .filter((element) => !overlapSkip(element) && textOf(element));
    const containsRect = (outer, inner) =>
      outer.left <= inner.left + 1 && outer.right >= inner.right - 1 &&
      outer.top <= inner.top + 1 && outer.bottom >= inner.bottom - 1;
    for (let index = 0; index < textNodes.length; index += 1) {
      const left = textNodes[index];
      const leftRect = left.getBoundingClientRect();
      for (let other = index + 1; other < textNodes.length; other += 1) {
        const right = textNodes[other];
        if (left.contains(right) || right.contains(left)) continue;
        const rightRect = right.getBoundingClientRect();
        if (containsRect(leftRect, rightRect) || containsRect(rightRect, leftRect)) continue;
        const overlapX = Math.min(leftRect.right, rightRect.right) - Math.max(leftRect.left, rightRect.left);
        const overlapY = Math.min(leftRect.bottom, rightRect.bottom) - Math.max(leftRect.top, rightRect.top);
        if (overlapX > 1 && overlapY > 1) {
          push(
            "ux_overlapping_text",
            `${describe(left)} overlaps ${describe(right)}`,
          );
        }
      }
    }

    const interactiveSelector = [
      "button", "a[href]", "input:not([type=hidden])", "select", "textarea",
      "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=switch]",
    ].join(",");
    for (const element of document.querySelectorAll(interactiveSelector)) {
      if (!visible(element)) continue;
      let target = element;
      if (element instanceof HTMLInputElement && ["checkbox", "radio"].includes(element.type)) {
        const explicit = element.id
          ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`)
          : null;
        const label = element.closest("label") || explicit;
        if (label && visible(label)) target = label;
      }
      const rect = target.getBoundingClientRect();
      // Browser font/layout rounding can report a 44px SDK control a fraction below 44.
      if (rect.width < 43.5 || rect.height < 43.5) {
        push(
          "ux_tap_target_too_small",
          `${describe(element)} has a ${Math.round(rect.width)}x${Math.round(rect.height)}px tap target; minimum is 44x44px`,
        );
      }
    }

    const primarySelector = [
      "[data-dot-primary-action]",
      "form button[type=submit]",
      "form input[type=submit]",
    ].join(",");
    for (const element of document.querySelectorAll(primarySelector)) {
      if (!visible(element)) continue;
      const rect = element.getBoundingClientRect();
      if (clipped(rect)) {
        push(
          "ux_primary_control_clipped",
          `${describe(element)} is clipped or outside the 390x844 mobile viewport`,
        );
      }
    }

    const fieldSelector = [
      "input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset]):not([type=image])",
      "select", "textarea",
    ].join(",");
    for (const control of document.querySelectorAll(fieldSelector)) {
      if (!visible(control)) continue;
      const id = control.getAttribute("id");
      const labelledBy = control.getAttribute("aria-labelledby");
      const wrappingLabel = control.closest("label");
      const explicitLabel = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
      const hasLabel = Boolean(
        control.getAttribute("aria-label")?.trim() ||
        (labelledBy && labelledBy.split(/\s+/).some((labelId) =>
          textOf(document.getElementById(labelId) || document.createElement("span")))) ||
        (wrappingLabel && textOf(wrappingLabel)) ||
        (explicitLabel && textOf(explicitLabel))
      );
      if (!hasLabel) {
        push("ux_missing_control_label", `${describe(control)} has no accessible label`);
      }
    }
    for (const control of document.querySelectorAll("button, [role=button], a[href]")) {
      if (!visible(control)) continue;
      const labelledBy = control.getAttribute("aria-labelledby");
      const hasName = Boolean(
        textOf(control) || control.getAttribute("aria-label")?.trim() ||
        control.getAttribute("title")?.trim() ||
        (labelledBy && labelledBy.split(/\s+/).some((labelId) =>
          textOf(document.getElementById(labelId) || document.createElement("span")))) ||
        [...control.querySelectorAll("img[alt], svg title")].some((item) =>
          String(item.getAttribute("alt") || item.textContent || "").trim())
      );
      if (!hasName) {
        push("ux_missing_control_label", `${describe(control)} has no accessible label`);
      }
    }

    const visibleText = [...document.body.querySelectorAll("h1,h2,h3,h4,p,label,button,a,li,th,td,legend,span,small,strong")]
      .filter(visible)
      .map(textOf)
      .filter(Boolean);
    for (const text of visibleText) {
      if (/(?:\b[a-z][a-z0-9]*_[a-z0-9_]+\b)/.test(text)) {
        push("ux_visible_identifier", `visible copy exposes a machine identifier: “${text.slice(0, 120)}”`);
      }
      if (/(?:\b(?:valid\s+)?json\b|\{\s*["'][^}]+:|\[\s*["'])/i.test(text)) {
        push("ux_raw_json_copy", `visible copy asks the user to work with raw JSON: “${text.slice(0, 120)}”`);
      }
      if (/\b(?:schema|entity|entities|record id|field type|database field|expected version)\b/i.test(text)) {
        push("ux_schema_admin_copy", `visible copy exposes implementation terminology: “${text.slice(0, 120)}”`);
      }
    }
    for (const control of document.querySelectorAll("input:not([type=hidden]), textarea")) {
      if (!visible(control)) continue;
      const placeholder = String(control.getAttribute("placeholder") || "").trim();
      if (/(?:\bjson\b|\{\s*["']|\[\s*["'])/i.test(placeholder)) {
        push("ux_raw_json_copy", `visible placeholder asks the user for raw JSON: “${placeholder.slice(0, 120)}”`);
      }
    }

    return { issues, viewport: { width: viewportWidth, height: viewportHeight } };
  });
}

async function assertVisibleExperience(frame) {
  const audit = await auditVisibleExperience(frame);
  if (audit.issues.length) {
    const error = new Error(audit.issues[0].message);
    error.code = audit.issues[0].code;
    error.uxIssues = audit.issues;
    throw error;
  }
  return audit;
}

async function assertFocused(locator, selector) {
  if (await locator.count() !== 1) {
    fail("acceptance_input_focus_lost", `field was remounted while typing: ${selector}`);
  }
  const focused = await locator.evaluate((element) => element === element.ownerDocument.activeElement);
  if (!focused) fail("acceptance_input_focus_lost", `field lost focus while typing: ${selector}`);
}

function browserTemporalValue(type, value) {
  const text = String(value).trim();
  if (type === "date") {
    const match = text.match(/^(\d{4}-\d{2}-\d{2})/);
    return match?.[1] || text;
  }
  if (type === "datetime-local") {
    const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})/);
    return match ? `${match[1]}T${match[2]}:${match[3]}` : text.replace(/Z$/, "");
  }
  if (type === "month") {
    const match = text.match(/^(\d{4}-\d{2})/);
    return match?.[1] || text;
  }
  if (type === "time") {
    const match = text.match(/(?:^|T)(\d{2}):(\d{2})/);
    return match ? `${match[1]}:${match[2]}` : text.replace(/Z$/, "");
  }
  return text.replace(/Z$/, "");
}

async function enterField(frame, selector, value) {
  const candidates = frame.locator(selector);
  const candidateCount = await candidates.count();
  if (!candidateCount) fail("acceptance_flow_missing", `required field was not rendered: ${selector}`);
  if (candidateCount > 1) {
    const choices = await candidates.evaluateAll((elements) => elements.map((element, index) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return {
        index,
        visible: style.display !== "none" && style.visibility !== "hidden" &&
          Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0,
        enabled: !(element instanceof HTMLInputElement) || !element.disabled,
        type: element instanceof HTMLInputElement ? element.type.toLowerCase() : "",
      };
    }));
    const enabledChoices = choices.filter((item) =>
      item.visible && item.enabled && ["checkbox", "radio"].includes(item.type)
    );
    if (enabledChoices.length !== choices.filter((item) => item.visible).length || !enabledChoices.length) {
      fail("acceptance_flow_ambiguous", `required field matched ${candidateCount} controls: ${selector}`);
    }
    const desiredCount = Array.isArray(value) && value.length
      ? Math.min(value.length, enabledChoices.length)
      : 1;
    const selected = [];
    for (const choice of enabledChoices.slice(0, desiredCount)) {
      const locator = candidates.nth(choice.index);
      if (!(await locator.isChecked())) await locator.click();
      if (!(await locator.isChecked())) {
        fail("acceptance_field_value_mismatch", `choice did not stay selected: ${selector}`);
      }
      selected.push(await locator.inputValue());
    }
    return { mode: "multi-choice", value: selected };
  }
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
    await locator.fill(browserTemporalValue(metadata.type, desired));
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

function seededRecordCount(records) {
  if (!records || typeof records !== "object") return 0;
  return Object.values(records).reduce(
    (total, list) => total + (Array.isArray(list) ? list.length : 0),
    0,
  );
}

async function toggleSeededCheckbox(page, frame, timeoutMs) {
  const checkbox = frame.locator('input[type="checkbox"]').first();
  if (!(await checkbox.count())) {
    fail("acceptance_flow_missing", "seeded records did not render a checkbox to toggle");
  }
  const before = await hostState(page);
  const beforeUpdates = before.operations.filter((item) => item.operation === "records.update").length;
  const beforeLists = before.operations.filter((item) => item.operation === "records.list").length;
  await checkbox.click();
  await page.waitForFunction(
    (minimum) => window.__DOT_ACCEPTANCE__.operations.filter(
      (item) => item.operation === "records.update",
    ).length > minimum,
    beforeUpdates,
    { timeout: Math.min(timeoutMs, 4_000) },
  ).catch(() => fail("acceptance_flow_missing", "checkbox toggle did not call records.update"));
  await page.waitForFunction(
    (minimum) => window.__DOT_ACCEPTANCE__.operations.filter(
      (item) => item.operation === "records.list",
    ).length > minimum,
    beforeLists,
    { timeout: Math.min(timeoutMs, 4_000) },
  ).catch(() => fail(
    "acceptance_records_refresh_missing",
    "checkbox toggle did not refresh records",
  ));
  const afterCheckbox = frame.locator('input[type="checkbox"]').first();
  if (!(await afterCheckbox.isChecked())) {
    fail("acceptance_field_value_mismatch", "checkbox did not stay checked after records.update");
  }
}

function mutationIdentityMatches(operation, step) {
  if (operation.operation !== step.operation) return false;
  if (typeof step.entity === "string" && operation.args?.entity !== step.entity) return false;
  return true;
}

function mutationPayloadMatches(operation, step) {
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

function payloadDiagnostics(operation, step) {
  const required = Array.isArray(step.required_payload_fields)
    ? step.required_payload_fields
    : Array.isArray(step.required_fields)
      ? step.required_fields
      : [];
  const allowed = Array.isArray(step.allowed_payload_fields)
    ? step.allowed_payload_fields
    : Array.isArray(step.allowed_fields)
      ? step.allowed_fields
      : [];
  const data = operation.args?.data;
  const observed = data && typeof data === "object" ? Object.keys(data).sort() : [];
  return `required=${JSON.stringify(required)} allowed=${JSON.stringify(allowed)} observed_keys=${JSON.stringify(observed)}`;
}

async function verifyMutationOutcome(
  page,
  frame,
  step,
  { beforeMutations, beforeLists, beforeText, interaction, timeoutMs, required },
) {
  await page.waitForFunction(
    (minimum) => window.__DOT_ACCEPTANCE__.operations.filter((item) => item.mutating).length >= minimum,
    beforeMutations + 1,
    { timeout: Math.min(timeoutMs, 4_000) },
  ).catch(async () => {
    const debugState = await hostState(page).catch(() => ({}));
    const body = await frame.locator("body").innerText().catch(() => "");
    fail(
      "acceptance_flow_missing",
      `${interaction} did not call ${step.operation}; state=${JSON.stringify(debugState)} body=${body.slice(0, 300)}`,
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
      `${interaction} produced ${newMutations.length} mutations; expected exactly one ${step.operation}`,
    );
  }
  if (!mutationIdentityMatches(newMutations[0], step)) {
    const expected = workflowIdentity(step);
    const observed = observedMutationIdentity(newMutations[0]);
    fail(
      "acceptance_workflow_mismatch",
      `${interaction} produced the wrong workflow mutation; expected ${workflowIdentityText(expected)}, observed ${workflowIdentityText(observed)}; mutation=${JSON.stringify(newMutations[0])}`,
    );
  }
  if (!mutationPayloadMatches(newMutations[0], step)) {
    fail(
      "acceptance_required_field_missing",
      `${interaction} produced an invalid payload for ${workflowIdentityText(workflowIdentity(step))}; ${payloadDiagnostics(newMutations[0], step)}`,
    );
  }

  let recordRefreshes = 0;
  let persistedRenders = 0;
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
    recordRefreshes = 1;
    await frame.waitForFunction(
      (previous) => document.body.innerText.trim() !== previous,
      beforeText,
      { timeout: 600 },
    ).catch(() => undefined);
    const afterText = (await frame.locator("body").innerText()).trim();
    if (afterText !== beforeText) persistedRenders = 1;
    else if (required) {
      fail(
        "acceptance_persisted_result_missing",
        `${step.entity} refreshed after save but the persisted result was not visible`,
      );
    }
  }
  return { recordRefreshes, persistedRenders };
}

async function runAcceptance(page, frame, acceptancePlan, timeoutMs) {
  const fieldTyping = [];
  const acceptance = [];
  const workflowReveals = [];
  let requiredMutationCount = 0;
  let refreshVerified = 0;
  let persistedRenderVerified = 0;
  for (const step of acceptancePlan) {
    if (!step || typeof step !== "object") continue;
    const required = step.required === true;
    const revealState = {
      clicks: 0,
      explored: [],
      seen: new Set(),
      discovery: null,
      exactTargetAttempted: false,
    };
    const interaction = await interactionTarget(page, frame, step, { required, revealState });
    workflowReveals.push(...revealState.explored);
    if (!interaction) {
      acceptance.push({ operation: step.operation, entity: step.entity, passed: !required });
      continue;
    }
    if (interaction.kind === "direct_action") {
      const verified = await verifyMutationOutcome(page, frame, step, {
        beforeMutations: interaction.beforeMutations,
        beforeLists: interaction.beforeLists,
        beforeText: interaction.beforeText,
        interaction: "direct workflow action",
        timeoutMs,
        required,
      });
      requiredMutationCount += required ? 1 : 0;
      refreshVerified += verified.recordRefreshes;
      persistedRenderVerified += verified.persistedRenders;
      acceptance.push({ operation: step.operation, entity: step.entity, passed: true });
      continue;
    }
    const target = interaction.target;
    const fieldHints = stepFieldHints(step);
    const targetId = `dot-acceptance-${++acceptanceTargetSequence}`;
    const targetSelector = await target.evaluate((element, testId) => {
      element.setAttribute("data-dot-acceptance-target", testId);
      return `[data-dot-acceptance-target="${testId}"]`;
    }, targetId);
    for (const [name, value] of Object.entries(fieldHints)) {
      const fieldSelector = selectorField(targetSelector, name);
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
          `visible submit button was not rendered inside the discovered ${step.entity || step.operation} form`,
        );
      }
      continue;
    }
    if (submitCount !== 1) {
      fail(
        "acceptance_flow_ambiguous",
        `discovered form contained ${submitCount} submit buttons`,
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
    const verified = await verifyMutationOutcome(page, frame, step, {
      beforeMutations,
      beforeLists,
      beforeText,
      interaction: `form submit ${JSON.stringify(submitMetadata)}`,
      timeoutMs,
      required,
    });
    requiredMutationCount += required ? 1 : 0;
    refreshVerified += verified.recordRefreshes;
    persistedRenderVerified += verified.persistedRenders;
    acceptance.push({ operation: step.operation, entity: step.entity, passed: true });
  }
  return {
    acceptance,
    field_typing: fieldTyping,
    workflow_reveals: workflowReveals,
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
  const initialRecords = request.records && typeof request.records === "object" ? request.records : {};
  const inner = iframeDocument(request.bundle, context, channelToken);
  await page.setContent(hostDocument(inner, context, channelToken, initialRecords), { waitUntil: "domcontentloaded" });
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
  if (seededRecordCount(initialRecords)) {
    await page.waitForFunction(
      () => window.__DOT_ACCEPTANCE__.operations.some((item) => item.operation === "records.list"),
      undefined,
      { timeout: Math.min(timeoutMs, 4_000) },
    ).catch(() => fail("acceptance_records_refresh_missing", "seeded records were not listed"));
  }
  const initialExperience = await assertVisibleExperience(frame);
  if (seededRecordCount(initialRecords)) {
    await toggleSeededCheckbox(page, frame, timeoutMs);
  }
  const acceptanceResult = await runAcceptance(page, frame, acceptancePlan, timeoutMs);
  await assertVisibleExperience(frame);
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
      records: state.records,
      ux_audit: { passed: true, viewport: initialExperience.viewport },
      ...acceptanceResult,
    },
  });
} catch (error) {
  const message = String(error?.message || error);
  const unavailable = /browserType\.launch|Failed to move to new namespace|chrome-sandbox/i.test(message);
  const timeout = /Timeout|timed out/i.test(message);
  const uxIssues = Array.isArray(error?.uxIssues)
    ? error.uxIssues.slice(0, 20).map((item) => issue(item.code, item.message))
    : null;
  respond({
    ok: false,
    issues: uxIssues || [issue(
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
