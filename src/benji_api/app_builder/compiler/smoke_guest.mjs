import { parseHTML } from "linkedom/worker";

const MAX_ERRORS = 12;
const MAX_TIMER_CALLBACKS = 100;
const MAX_STATIC_HTML_BYTES = 256_000;
const MAX_JSON_DEPTH = 12;
const MAX_JSON_NODES = 2_000;

// This module and generated code intentionally share one QuickJS realm. Capture trusted
// operations before the generated bundle can replace JSON.stringify or poison prototypes.
const reflectApply = Reflect.apply;
const objectCreate = Object.create;
const objectDefineProperties = Object.defineProperties;
const objectFreeze = Object.freeze;
const objectGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
const objectGetOwnPropertyDescriptors = Object.getOwnPropertyDescriptors;
const objectGetPrototypeOf = Object.getPrototypeOf;
const objectKeys = Object.keys;
const objectPrototype = Object.prototype;
const arrayFrom = Array.from;
const arrayIsArray = Array.isArray;
const arrayPushMethod = Array.prototype.push;
const arrayIncludesMethod = Array.prototype.includes;
const arraySliceMethod = Array.prototype.slice;
const stringCodePointAtMethod = String.prototype.codePointAt;
const stringPadStartMethod = String.prototype.padStart;
const stringReplaceMethod = String.prototype.replace;
const stringSliceMethod = String.prototype.slice;
const mapDeleteMethod = Map.prototype.delete;
const mapEntriesMethod = Map.prototype.entries;
const mapGetMethod = Map.prototype.get;
const mapSetMethod = Map.prototype.set;
const setHasMethod = Set.prototype.has;
const jsonParse = JSON.parse;
const jsonStringify = JSON.stringify;
const stringConvert = String;
const numberConvert = Number;
const errorConstructor = Error;
const promiseResolve = Promise.resolve.bind(Promise);
const numberIsFinite = Number.isFinite;
const mathMax = Math.max;

const arrayPush = (target, value) => reflectApply(arrayPushMethod, target, [value]);
const arraySlice = (target, start, end) => reflectApply(arraySliceMethod, target, [start, end]);
const stringCodePointAt = (value, index) => reflectApply(stringCodePointAtMethod, value, [index]);
const stringPadStart = (value, length, fill) => reflectApply(stringPadStartMethod, value, [length, fill]);
const stringReplace = (value, pattern, replacement) => reflectApply(stringReplaceMethod, value, [pattern, replacement]);
const stringSlice = (value, start, end) => reflectApply(stringSliceMethod, value, [start, end]);
const mapDelete = (target, key) => reflectApply(mapDeleteMethod, target, [key]);
const mapEntries = (target) => reflectApply(mapEntriesMethod, target, []);
const mapGet = (target, key) => reflectApply(mapGetMethod, target, [key]);
const mapSet = (target, key, value) => reflectApply(mapSetMethod, target, [key, value]);
const setHas = (target, value) => reflectApply(setHasMethod, target, [value]);

const READ_ONLY_OPERATIONS = objectFreeze(new Set(["app.data.get", "records.list"]));
const MUTATIONS = objectFreeze(new Set([
  "records.create",
  "records.update",
  "records.delete",
  "dot.reminder.create",
]));

const state = {
  activeGesture: null,
  errors: [],
  interactionTargetFound: null,
  nextRecord: 1,
  nextRequest: 1,
  nextIdempotency: 1,
  pendingRequests: new Map(),
  ready: false,
  recordReadAfterWrite: false,
  recordReads: 0,
  records: new Map(),
  recordWrites: 0,
  requestIdsScoped: true,
  idempotencyIdsScoped: true,
  operations: [],
  runtimeNonce: null,
  requiredAcceptance: [],
};

function ownData(value, name) {
  if (!value || (typeof value !== "object" && typeof value !== "function")) return undefined;
  const descriptor = objectGetOwnPropertyDescriptor(value, name);
  return descriptor && "value" in descriptor ? descriptor.value : undefined;
}

function safeJsonCopy(value, depth = 0, budget = { nodes: 0 }) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return numberIsFinite(value) ? value : null;
  if (value === undefined) return undefined;
  if (typeof value !== "object" || depth > MAX_JSON_DEPTH || budget.nodes >= MAX_JSON_NODES) {
    throw new errorConstructor("generated app request contains invalid data");
  }
  budget.nodes += 1;
  if (arrayIsArray(value)) {
    const result = [];
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = objectGetOwnPropertyDescriptor(value, stringConvert(index));
      const item = descriptor && "value" in descriptor
        ? safeJsonCopy(descriptor.value, depth + 1, budget)
        : null;
      arrayPush(result, item === undefined ? null : item);
    }
    return result;
  }
  const prototype = objectGetPrototypeOf(value);
  if (prototype !== objectPrototype && prototype !== null) {
    throw new errorConstructor("generated app request contains an unsupported object");
  }
  const result = objectCreate(null);
  const descriptors = objectGetOwnPropertyDescriptors(value);
  for (const key of objectKeys(descriptors)) {
    const descriptor = descriptors[key];
    if (!descriptor.enumerable || !("value" in descriptor)) continue;
    const item = safeJsonCopy(descriptor.value, depth + 1, budget);
    if (item !== undefined) result[key] = item;
  }
  return result;
}

function issue(code, message) {
  return { code, message: stringSlice(stringConvert(message), 0, 800) };
}

function utf8ByteLength(value) {
  let bytes = 0;
  for (const character of value) {
    const codePoint = stringCodePointAt(character, 0);
    if (codePoint <= 0x7f) bytes += 1;
    else if (codePoint <= 0x7ff) bytes += 2;
    else if (codePoint <= 0xffff) bytes += 3;
    else bytes += 4;
  }
  return bytes;
}

function errorText(value) {
  if (value && typeof value === "object") {
    const stack = ownData(value, "stack");
    const message = ownData(value, "message");
    if (typeof stack === "string") return stack;
    if (typeof message === "string") return message;
  }
  try { return stringConvert(value); } catch { return "generated app runtime error"; }
}

function capture(code, value) {
  if (state.errors.length >= MAX_ERRORS) return;
  arrayPush(state.errors, issue(code, errorText(value)));
}

const { window } = parseHTML(
  "<!doctype html><html><head><meta charset=\"utf-8\"></head><body><div id=\"dot-app-root\"></div></body></html>",
);
// React selects a legacy IE value-change polyfill when `oninput` is absent from the synthetic
// document. LinkeDOM dispatches standards-based input events, so advertise that capability before
// the generated React bundle initializes its event system.
objectDefineProperties(window.document, {
  oninput: { configurable: true, enumerable: false, writable: true, value: null },
});
const windowDispatchEvent = window.dispatchEvent.bind(window);
const windowAddEventListener = window.addEventListener.bind(window);
const documentQuerySelector = window.document.querySelector.bind(window.document);
const elementQuerySelectorMethod = window.Element.prototype.querySelector;
const eventConstructor = window.Event;
const inputValueSetter = objectGetOwnPropertyDescriptor(
  window.HTMLInputElement?.prototype,
  "value",
)?.set;
const inputValueGetter = objectGetOwnPropertyDescriptor(
  window.HTMLInputElement?.prototype,
  "value",
)?.get;
const inputCheckedSetter = objectGetOwnPropertyDescriptor(
  window.HTMLInputElement?.prototype,
  "checked",
)?.set;
const inputCheckedGetter = objectGetOwnPropertyDescriptor(
  window.HTMLInputElement?.prototype,
  "checked",
)?.get;
const textareaValueSetter = objectGetOwnPropertyDescriptor(
  window.HTMLTextAreaElement?.prototype,
  "value",
)?.set;
const textareaValueGetter = objectGetOwnPropertyDescriptor(
  window.HTMLTextAreaElement?.prototype,
  "value",
)?.get;
const selectValueSetter = objectGetOwnPropertyDescriptor(
  window.HTMLSelectElement?.prototype,
  "value",
)?.set;
const selectValueGetter = objectGetOwnPropertyDescriptor(
  window.HTMLSelectElement?.prototype,
  "value",
)?.get;
let domPrototype = window.document.getElementById("dot-app-root");
let rootInnerHTMLGetter;
let rootTextContentGetter;
let rootChildNodesGetter;
let targetDispatchEventMethod;
while (domPrototype) {
  const innerHTMLDescriptor = objectGetOwnPropertyDescriptor(domPrototype, "innerHTML");
  if (!rootInnerHTMLGetter && typeof innerHTMLDescriptor?.get === "function") rootInnerHTMLGetter = innerHTMLDescriptor.get;
  const textDescriptor = objectGetOwnPropertyDescriptor(domPrototype, "textContent");
  if (!rootTextContentGetter && typeof textDescriptor?.get === "function") rootTextContentGetter = textDescriptor.get;
  const childrenDescriptor = objectGetOwnPropertyDescriptor(domPrototype, "childNodes");
  if (!rootChildNodesGetter && typeof childrenDescriptor?.get === "function") rootChildNodesGetter = childrenDescriptor.get;
  const dispatchDescriptor = objectGetOwnPropertyDescriptor(domPrototype, "dispatchEvent");
  if (!targetDispatchEventMethod && typeof dispatchDescriptor?.value === "function") targetDispatchEventMethod = dispatchDescriptor.value;
  domPrototype = objectGetPrototypeOf(domPrototype);
}
const timerQueue = new Map();
let nextTimerId = 1;
let timerCallbacks = 0;

function scheduleTimer(callback, delay = 0, repeat = false, args = []) {
  const id = nextTimerId++;
  mapSet(timerQueue, id, {
    callback,
    delay: mathMax(0, numberConvert(delay) || 0),
    repeat,
    args,
  });
  return id;
}

function clearTimer(id) {
  mapDelete(timerQueue, numberConvert(id));
}

function runTimers(maxDelay = 100) {
  let ran = 0;
  const entries = arrayFrom(mapEntries(timerQueue));
  for (const entry of entries) {
    const id = entry[0];
    const timer = entry[1];
    if (timer.delay > maxDelay) continue;
    if (timerCallbacks >= MAX_TIMER_CALLBACKS) {
      capture("browser_timer_limit", "app scheduled too many timer callbacks during startup");
      break;
    }
    if (!timer.repeat) mapDelete(timerQueue, id);
    try {
      reflectApply(timer.callback, undefined, timer.args);
      ran += 1;
      timerCallbacks += 1;
    } catch (error) {
      capture("browser_runtime_error", error);
    }
  }
  return ran;
}

function messageEvent(data) {
  const event = new eventConstructor("message");
  objectDefineProperties(event, {
    data: { configurable: true, enumerable: true, value: data },
    source: { configurable: true, enumerable: true, value: window },
  });
  return event;
}

function recordResult(operation, args) {
  const entityValue = ownData(args, "entity");
  const entity = typeof entityValue === "string" ? entityValue : "record";
  const existing = mapGet(state.records, entity) || [];
  if (operation === "records.list") {
    state.recordReads += 1;
    if (state.recordWrites > 0) state.recordReadAfterWrite = true;
    return { data: arraySlice(existing), meta: { total: existing.length } };
  }
  if (operation === "records.create") {
    const data = ownData(args, "data");
    const record = {
      ...(data && typeof data === "object" ? data : {}),
      id: `00000000-0000-4000-8000-${stringPadStart(stringConvert(state.nextRecord++), 12, "0")}`,
      entity,
      version: 1,
      created_at: "2026-01-01T09:00:00Z",
      updated_at: "2026-01-01T09:00:00Z",
    };
    const records = arraySlice(existing);
    arrayPush(records, record);
    mapSet(state.records, entity, records);
    return record;
  }
  if (operation === "records.update") {
    const recordId = ownData(args, "record_id");
    const data = ownData(args, "data");
    return {
      ...(data && typeof data === "object" ? data : {}),
      id: typeof recordId === "string" ? recordId : "00000000-0000-4000-8000-000000000001",
      entity,
      version: 2,
      created_at: "2026-01-01T09:00:00Z",
      updated_at: "2026-01-01T09:00:00Z",
    };
  }
  if (operation === "records.delete") return { deleted: true };
  if (operation === "app.data.get") return {};
  return { ok: true };
}

function postMessage(message) {
  if (!message || typeof message !== "object") return;
  const type = ownData(message, "type");
  if (type === "dot.app.ready") {
    state.ready = true;
    return;
  }
  if (type === "dot.app.error") {
    const error = ownData(message, "error");
    capture("browser_runtime_error", ownData(error, "message") || "app reported a runtime error");
    return;
  }
  if (type !== "dot.app.request") return;
  const operation = ownData(message, "operation");
  const guestRequestId = ownData(message, "request_id");
  if (typeof operation !== "string" || typeof guestRequestId !== "string" || !state.runtimeNonce) {
    capture("invalid_runtime_request", "app emitted an invalid request");
    return;
  }
  arrayPush(state.operations, {
    operation,
    entity: typeof ownData(ownData(message, "args"), "entity") === "string"
      ? ownData(ownData(message, "args"), "entity")
      : null,
    data: safeJsonCopy(ownData(ownData(message, "args"), "data") ?? objectCreate(null)),
  });
  const hostRequestId = `${state.runtimeNonce}_req_${state.nextRequest++}`;
  mapSet(state.pendingRequests, hostRequestId, guestRequestId);
  state.requestIdsScoped = state.requestIdsScoped && hostRequestId.startsWith(`${state.runtimeNonce}_req_`);
  const suppliedIdempotency = ownData(message, "idempotency_key");
  if (typeof suppliedIdempotency === "string" || setHas(MUTATIONS, operation)) {
    const idempotencyKey = `${state.runtimeNonce}_idem_${state.nextIdempotency++}`;
    state.idempotencyIdsScoped = state.idempotencyIdsScoped && idempotencyKey.startsWith(`${state.runtimeNonce}_idem_`);
  }

  if (!setHas(READ_ONLY_OPERATIONS, operation)) {
    if (operation.startsWith("records.")) state.recordWrites += 1;
    const gesture = state.activeGesture;
    if (!gesture || gesture.used) {
      capture("background_mutation", `${operation} ran without a single-use user gesture`);
    } else {
      gesture.used = true;
    }
  }
  let args;
  try {
    args = safeJsonCopy(ownData(message, "args") ?? objectCreate(null));
  } catch (error) {
    capture("invalid_runtime_request", error);
    args = objectCreate(null);
  }
  const response = {
    protocol_version: 1,
    type: "dot.app.response",
    request_id: mapGet(state.pendingRequests, hostRequestId),
    ok: true,
    result: recordResult(operation, args),
  };
  mapDelete(state.pendingRequests, hostRequestId);
  windowDispatchEvent(messageEvent(response));
}

objectDefineProperties(window, {
  parent: { configurable: false, writable: false, value: window },
  postMessage: { configurable: false, writable: false, value: postMessage },
});

const safeConsole = objectFreeze({
  log() {},
  info() {},
  debug() {},
  warn() {},
  error(...args) {
    let message = "";
    for (const value of args) message += `${message ? " " : ""}${errorText(value)}`;
    capture("browser_console_error", message);
  },
});

const globals = {
  window,
  self: window,
  document: window.document,
  navigator: objectFreeze({ userAgent: "DotAppSmoke/QuickJS", language: "en" }),
  location: objectFreeze({ href: "https://generated.invalid/", origin: "https://generated.invalid", protocol: "https:" }),
  console: safeConsole,
  Node: window.Node,
  Element: window.Element,
  HTMLElement: window.HTMLElement,
  SVGElement: window.SVGElement,
  DocumentFragment: window.DocumentFragment,
  Event: window.Event,
  CustomEvent: window.CustomEvent,
  MutationObserver: window.MutationObserver,
  getComputedStyle: () => ({ display: "block", getPropertyValue: () => "" }),
  setTimeout: (callback, delay, ...args) => scheduleTimer(callback, delay, false, args),
  clearTimeout: clearTimer,
  setInterval: (callback, delay, ...args) => scheduleTimer(callback, delay, true, args),
  clearInterval: clearTimer,
  setImmediate: (callback, ...args) => scheduleTimer(callback, 0, false, args),
  clearImmediate: clearTimer,
  queueMicrotask: (callback) => promiseResolve().then(callback),
  requestAnimationFrame: (callback) => scheduleTimer(() => callback(Date.now()), 0),
  cancelAnimationFrame: clearTimer,
  crypto: objectFreeze({
    randomUUID() {
      const suffix = stringPadStart(stringConvert(state.nextRecord++), 12, "0");
      return `00000000-0000-4000-8000-${suffix}`;
    },
  }),
};

Object.assign(globalThis, globals);
Object.assign(window, globals);

windowAddEventListener("error", (event) => {
  capture("browser_runtime_error", ownData(event, "error") || ownData(event, "message") || "window error");
});
windowAddEventListener("unhandledrejection", (event) => {
  capture("browser_runtime_error", ownData(event, "reason") || "unhandled promise rejection");
});

const dotSmokeInitialize = (encoded) => {
  const value = jsonParse(encoded);
  const runtimeNonce = ownData(value, "runtime_nonce");
  if (
    state.runtimeNonce ||
    typeof runtimeNonce !== "string" ||
    !/^[a-zA-Z0-9_-]{16,96}$/.test(runtimeNonce)
  ) throw new errorConstructor("smoke runtime nonce is invalid");
  state.runtimeNonce = runtimeNonce;
  const requiredAcceptance = ownData(value, "required_acceptance");
  if (arrayIsArray(requiredAcceptance)) {
    state.requiredAcceptance = safeJsonCopy(requiredAcceptance);
  }
  return true;
};

const dotSmokeStep = () => runTimers(100);

function setControlValue(target, value) {
  // Browsers expose the default type of `<input>` as "text". LinkeDOM exposes null, which makes
  // React's ChangeEventPlugin classify an otherwise normal text input as unsupported.
  if (target instanceof window.HTMLInputElement && !target.type) target.type = "text";
  let setter;
  if (target instanceof window.HTMLInputElement) setter = inputValueSetter;
  else if (target instanceof window.HTMLTextAreaElement) setter = textareaValueSetter;
  else if (target instanceof window.HTMLSelectElement) setter = selectValueSetter;
  if (typeof setter === "function") reflectApply(setter, target, [value]);
  else target.value = value;
}

function setControlChecked(target, value) {
  if (target instanceof window.HTMLInputElement && typeof inputCheckedSetter === "function") {
    reflectApply(inputCheckedSetter, target, [value]);
  } else {
    target.checked = value;
  }
}

const dotSmokeInteract = (encoded) => {
  const request = jsonParse(encoded);
  const selectorValue = ownData(request, "selector");
  const eventTypeValue = ownData(request, "event_type");
  const gestureNonce = ownData(request, "gesture_nonce");
  if (
    !state.runtimeNonce ||
    typeof gestureNonce !== "string" ||
    !/^gesture_[a-zA-Z0-9_-]{16,128}$/.test(gestureNonce)
  ) throw new errorConstructor("smoke gesture nonce is invalid");
  const selector = typeof selectorValue === "string" && selectorValue.length <= 500
    ? selectorValue
    : "button, [role='button'], input[type='button'], input[type='submit'], a[href]";
  const target = documentQuerySelector(selector);
  state.interactionTargetFound = Boolean(target);
  if (!target) return false;
  const fields = ownData(request, "fields");
  if (fields && typeof fields === "object" && eventTypeValue !== "submit") {
    const descriptors = objectGetOwnPropertyDescriptors(fields);
    for (const name of objectKeys(descriptors)) {
      const descriptor = descriptors[name];
      if (!("value" in descriptor)) continue;
      const safeName = stringReplace(name, /[^a-zA-Z0-9_-]/g, "");
      const field = reflectApply(elementQuerySelectorMethod, target, [`[name="${safeName}"]`]);
      if (!field) continue;
      if (typeof descriptor.value === "boolean") setControlChecked(field, descriptor.value);
      else setControlValue(field, stringConvert(descriptor.value));
    }
  }
  const value = ownData(request, "value");
  const checked = ownData(request, "checked");
  if (typeof value === "string") setControlValue(target, value);
  if (typeof checked === "boolean") setControlChecked(target, checked);
  const gesture = {
    nonce: gestureNonce,
    used: false,
  };
  state.activeGesture = gesture;
  try {
    const primaryEventType = typeof eventTypeValue === "string" ? eventTypeValue : "click";
    reflectApply(targetDispatchEventMethod, target, [new eventConstructor(
      primaryEventType,
      { bubbles: true, cancelable: true },
    )]);
    // React normalizes text-control changes from native input/change events. LinkeDOM does not
    // reproduce every browser heuristic used by that plugin, so exercise both events after using
    // the native value setter. This updates either onInput or controlled onChange handlers without
    // mutating the field again between events.
    if (primaryEventType === "input") {
      reflectApply(targetDispatchEventMethod, target, [new eventConstructor(
        "change",
        { bubbles: true, cancelable: true },
      )]);
    }
  } catch (error) {
    capture("browser_runtime_error", error);
  } finally {
    if (state.activeGesture === gesture) state.activeGesture = null;
  }
  return true;
};

const dotSmokeClick = dotSmokeInteract;
// Reserved for blueprint-derived acceptance flows. The outer harness owns the interaction plan
// and gesture nonces; generated code never receives this control handle.
const dotSmokeAccept = dotSmokeInteract;

const dotSmokeResult = () => {
  const root = documentQuerySelector("#dot-app-root");
  const childNodes = root ? reflectApply(rootChildNodesGetter, root, []) : null;
  const textContent = root ? reflectApply(rootTextContentGetter, root, []) : "";
  const rendered = Boolean(root && (childNodes?.length || textContent));
  const staticHtml = root ? reflectApply(rootInnerHTMLGetter, root, []) : "";
  if (!state.ready) capture("browser_not_ready", "app did not emit dot.app.ready");
  if (state.ready && !rendered) {
    capture("browser_not_rendered", "app became ready without rendering content");
  }
  if (utf8ByteLength(staticHtml) > MAX_STATIC_HTML_BYTES) {
    capture("static_render_too_large", "isolated app HTML exceeds 256 KB");
  }
  if (state.recordWrites > 0 && !state.recordReadAfterWrite && state.recordReads > 0) {
    capture("record_write_not_invalidated", "record write did not refresh subscribed data");
  }
  for (const step of state.requiredAcceptance) {
    if (ownData(step, "required") !== true) continue;
    const operation = ownData(step, "operation");
    const entity = ownData(step, "entity");
    let observed = null;
    for (const candidate of state.operations) {
      if (
        ownData(candidate, "operation") === operation &&
        (typeof entity !== "string" || ownData(candidate, "entity") === entity)
      ) {
        observed = candidate;
        break;
      }
    }
    if (!observed) {
      capture(
        "acceptance_flow_missing",
        `compiled app did not exercise required ${operation}${typeof entity === "string" ? ` flow for ${entity}` : " flow"}`,
      );
      continue;
    }
    const data = ownData(observed, "data");
    const requiredFields = ownData(step, "required_fields");
    if (arrayIsArray(requiredFields)) {
      for (const field of requiredFields) {
        const submitted = ownData(data, field);
        if (typeof field !== "string" || submitted === undefined || submitted === null) {
          capture("acceptance_required_field_missing", `required ${entity}.${field} was not submitted`);
        }
      }
    }
    const allowedFields = ownData(step, "allowed_fields");
    if (arrayIsArray(allowedFields) && data && typeof data === "object") {
      for (const field of objectKeys(objectGetOwnPropertyDescriptors(data))) {
        if (!reflectApply(arrayIncludesMethod, allowedFields, [field])) {
          capture("acceptance_unknown_field", `undeclared ${entity}.${field} was submitted`);
        }
      }
    }
  }
  // Acceptance checks may append errors after the first copy above.
  const finalIssues = arraySlice(state.errors, 0, MAX_ERRORS);
  return jsonStringify({
    ok: finalIssues.length === 0,
    issues: finalIssues,
    result: {
      ready: state.ready,
      rendered,
      // Do not serialize an oversized render into the subprocess response.
      static_html: utf8ByteLength(staticHtml) <= MAX_STATIC_HTML_BYTES ? staticHtml : "",
      runtime_errors: state.errors.length,
      record_read_exercised: state.recordReads > 0,
      record_write_exercised: state.recordWrites > 0,
      record_invalidation_exercised: state.recordReadAfterWrite,
      interaction_target_found: state.interactionTargetFound,
      record_reads: state.recordReads,
      record_writes: state.recordWrites,
      request_ids_runtime_scoped: state.requestIdsScoped,
      idempotency_ids_runtime_scoped: state.idempotencyIdsScoped,
      operations: arraySlice(state.operations),
    },
  });
};

const dotSmokeFind = (selector) => {
  if (typeof selector !== "string" || selector.length > 500) return false;
  return Boolean(documentQuerySelector(selector));
};

const dotSmokeControlValue = (selector) => {
  if (typeof selector !== "string" || selector.length > 500) {
    return jsonStringify({ found: false });
  }
  const target = documentQuerySelector(selector);
  if (!target) return jsonStringify({ found: false });
  if (target instanceof window.HTMLInputElement && target.type === "checkbox") {
    const checked = typeof inputCheckedGetter === "function"
      ? reflectApply(inputCheckedGetter, target, [])
      : target.checked;
    return jsonStringify({ found: true, checked: Boolean(checked) });
  }
  let getter;
  if (target instanceof window.HTMLInputElement) getter = inputValueGetter;
  else if (target instanceof window.HTMLTextAreaElement) getter = textareaValueGetter;
  else if (target instanceof window.HTMLSelectElement) getter = selectValueGetter;
  const value = typeof getter === "function" ? reflectApply(getter, target, []) : target.value;
  return jsonStringify({ found: true, value: stringConvert(value ?? "") });
};

for (const intrinsic of [
  Object.prototype,
  Array.prototype,
  Function.prototype,
  String.prototype,
  Number.prototype,
  Boolean.prototype,
  RegExp.prototype,
  Date.prototype,
  Map.prototype,
  Set.prototype,
  WeakMap.prototype,
  Promise.prototype,
  JSON,
  Reflect,
]) {
  try { objectFreeze(intrinsic); } catch {}
}
domPrototype = objectGetPrototypeOf(window.document.getElementById("dot-app-root"));
while (domPrototype && domPrototype !== objectPrototype) {
  try { objectFreeze(domPrototype); } catch {}
  domPrototype = objectGetPrototypeOf(domPrototype);
}

objectDefineProperties(globalThis, {
  // The outer harness captures these function handles and immediately deletes the properties
  // before evaluating generated code. The app therefore cannot forge a gesture or test result.
  __dotSmokeInitialize: { configurable: true, writable: false, value: dotSmokeInitialize },
  __dotSmokeStep: { configurable: true, writable: false, value: dotSmokeStep },
  __dotSmokeClick: { configurable: true, writable: false, value: dotSmokeClick },
  __dotSmokeAccept: { configurable: true, writable: false, value: dotSmokeAccept },
  __dotSmokeFind: { configurable: true, writable: false, value: dotSmokeFind },
  __dotSmokeControlValue: { configurable: true, writable: false, value: dotSmokeControlValue },
  __dotSmokeResult: { configurable: true, writable: false, value: dotSmokeResult },
});
