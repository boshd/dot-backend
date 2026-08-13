import React, {
  Component,
  type ErrorInfo,
  type ReactNode,
  useCallback,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

const PROTOCOL_VERSION = 1;
const SDK_VERSION = "2";
const REQUEST_TIMEOUT_MS = 20_000;
const MAX_RECORD_PAGE_SIZE = 100;

type JsonObject = Record<string, unknown>;
type RuntimeError = { code: string; message: string };
type RequestOptions = { idempotencyKey?: string; timeoutMs?: number };

export type AppRecord<T = JsonObject> = T & {
  id: string;
  entity: string;
  version: number;
  created_at?: string | null;
  updated_at?: string | null;
};

/** @internal */
declare global {
  interface Window {
    __DOT_APP_CHANNEL_TOKEN__?: string;
    __DOT_APP_POST__?: (message: JsonObject) => void;
  }
}

type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timeout: ReturnType<typeof setTimeout>;
};

const pending = new Map<string, PendingRequest>();
const contextListeners = new Set<() => void>();
const recordListeners = new Map<string, Set<() => void>>();
let appData: unknown = undefined;
let initialized = false;

function requestId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `dot_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

function post(message: JsonObject): void {
  const envelope = {
    ...message,
    protocol_version: PROTOCOL_VERSION,
    sdk_version: SDK_VERSION,
    channel_token: window.__DOT_APP_CHANNEL_TOKEN__,
  };
  // The browser runtime relays same-window messages through a private
  // MessagePort. Generated code never receives that port, so the trusted
  // bootstrap can bind mutations to real browser events. The QuickJS build
  // and smoke runtimes intentionally keep the parent bridge fallback.
  if (window.__DOT_APP_CHANNEL_TOKEN__ && window.__DOT_APP_POST__) {
    window.__DOT_APP_POST__(envelope);
  }
  else window.parent.postMessage(envelope, "*");
}

function runtimeError(value: unknown): Error {
  if (value && typeof value === "object") {
    const candidate = value as Partial<RuntimeError>;
    if (typeof candidate.message === "string") {
      const error = new Error(candidate.message);
      error.name = typeof candidate.code === "string" ? candidate.code : "DotAppRuntimeError";
      return error;
    }
  }
  return new Error("Dot couldn't complete that app action");
}

function bridgeRequest<T = unknown>(
  operation: string,
  args: JsonObject = {},
  options: RequestOptions = {},
): Promise<T> {
  if (!/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}$/.test(operation)) {
    return Promise.reject(new Error("Invalid Dot app operation"));
  }
  const request_id = requestId();
  return new Promise<T>((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(request_id);
      reject(new Error("Dot app action timed out"));
    }, options.timeoutMs ?? REQUEST_TIMEOUT_MS);
    pending.set(request_id, { resolve: resolve as (value: unknown) => void, reject, timeout });
    post({
      type: "dot.app.request",
      request_id,
      operation,
      args,
      idempotency_key: options.idempotencyKey,
    });
  });
}

function receive(event: MessageEvent): void {
  if (event.source !== window.parent || !event.data || typeof event.data !== "object") return;
  const message = event.data as JsonObject;
  if (message.protocol_version !== PROTOCOL_VERSION) return;
  if (message.type === "dot.app.context") {
    appData = message.data;
    initialized = true;
    contextListeners.forEach((listener) => listener());
    return;
  }
  if (message.type !== "dot.app.response" || typeof message.request_id !== "string") return;
  const request = pending.get(message.request_id);
  if (!request) return;
  clearTimeout(request.timeout);
  pending.delete(message.request_id);
  if (message.ok === true) request.resolve(message.result);
  else request.reject(runtimeError(message.error));
}

if (typeof window !== "undefined") window.addEventListener("message", receive);

function subscribeToAppData(listener: () => void): () => void {
  contextListeners.add(listener);
  return () => contextListeners.delete(listener);
}

function appDataSnapshot(): unknown {
  return appData;
}

function subscribeToRecords(entity: string, listener: () => void): () => void {
  const listeners = recordListeners.get(entity) ?? new Set<() => void>();
  listeners.add(listener);
  recordListeners.set(entity, listeners);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) recordListeners.delete(entity);
  };
}

function invalidateRecords(entity: unknown): void {
  if (typeof entity === "string") {
    recordListeners.get(entity)?.forEach((listener) => listener());
    return;
  }
  recordListeners.forEach((listeners) => listeners.forEach((listener) => listener()));
}

/** @internal */
export function DotRuntimeProvider({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

export function useAppData<T = unknown>() {
  const data = useSyncExternalStore(subscribeToAppData, appDataSnapshot, appDataSnapshot) as
    | T
    | undefined;
  const [loading, setLoading] = useState(!initialized);
  const [error, setError] = useState<Error | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await bridgeRequest<T>("app.data.get");
      appData = result;
      initialized = true;
      contextListeners.forEach((listener) => listener());
      return result;
    } catch (caught) {
      const nextError = caught instanceof Error ? caught : new Error(String(caught));
      setError(nextError);
      throw nextError;
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!initialized) void refresh().catch(() => undefined);
    else setLoading(false);
  }, [refresh]);
  return { data, loading, error, refresh } as const;
}

export function useRecords<T = JsonObject>(
  entity: string,
  query: { limit?: number; offset?: number } = {},
) {
  const [records, setRecords] = useState<AppRecord<T>[]>([]);
  const [meta, setMeta] = useState<JsonObject>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  // Keep generated apps inside the host API contract. Code generation can ask
  // for an optimistic page size, but a harmless oversized read must not turn a
  // successful write into a refresh timeout or a misleading save failure.
  const limit = Math.min(
    MAX_RECORD_PAGE_SIZE,
    Math.max(1, Math.trunc(query.limit ?? 50)),
  );
  const offset = Math.max(0, Math.trunc(query.offset ?? 0));
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await bridgeRequest<unknown>("records.list", { entity, limit, offset });
      const envelope = result as { data?: unknown; meta?: JsonObject };
      const nextRecords = Array.isArray(envelope?.data)
        ? envelope.data
        : Array.isArray(result)
          ? result
          : [];
      setRecords(nextRecords as AppRecord<T>[]);
      setMeta(envelope?.meta ?? {});
      return nextRecords as AppRecord<T>[];
    } catch (caught) {
      const nextError = caught instanceof Error ? caught : new Error(String(caught));
      setError(nextError);
      throw nextError;
    } finally {
      setLoading(false);
    }
  }, [entity, limit, offset]);
  useEffect(() => {
    const unsubscribe = subscribeToRecords(entity, () => void refresh().catch(() => undefined));
    void refresh().catch(() => undefined);
    return unsubscribe;
  }, [entity, refresh]);
  return { records, meta, loading, error, refresh } as const;
}

export async function runAction<T = unknown>(
  operation: string,
  args: JsonObject = {},
  options: RequestOptions = {},
): Promise<T> {
  const idempotencyKey =
    options.idempotencyKey ?? (operation === "records.list" ? undefined : requestId());
  const result = await bridgeRequest<T>(operation, args, { ...options, idempotencyKey });
  if (["records.create", "records.update", "records.delete"].includes(operation)) {
    // Updates and deletes identify a record rather than an entity, so invalidate all
    // subscribed collections when the caller cannot name the affected entity.
    invalidateRecords(operation === "records.create" ? args.entity : undefined);
  }
  return result;
}

/** @internal */
export function __dotInitialize(initialData: unknown): void {
  if (initialData !== undefined) {
    appData = initialData;
    initialized = true;
  }
}

/** @internal */
export function __dotNotifyReady(): void {
  post({ type: "dot.app.ready" });
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => {
      post({
        type: "dot.app.resize",
        height: Math.ceil(document.documentElement.scrollHeight),
      });
    });
    observer.observe(document.documentElement);
  }
}

type BoundaryProps = { children: ReactNode };
type BoundaryState = { error: Error | null };

/** @internal */
export class DotRuntimeErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    post({
      type: "dot.app.error",
      error: { name: error.name, message: error.message, component_stack: info.componentStack },
    });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="dot-runtime-error" role="alert">
        <p className="dot-overline">dot app</p>
        <h1>this app hit a snag</h1>
        <p>try reopening it. dot has the details if it needs a repair.</p>
      </main>
    );
  }
}
