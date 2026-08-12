import React from "react";
import { createRoot } from "react-dom/client";
import GeneratedApp from "__dot_user_entry__";
import {
  __dotInitialize,
  __dotNotifyReady,
  DotRuntimeErrorBoundary,
  DotRuntimeProvider,
} from "@dot/app-runtime";
import "@dot/ui/styles.css";

declare global {
  interface Window {
    __DOT_APP_CONTEXT__?: { data?: unknown };
  }
}

__dotInitialize(window.__DOT_APP_CONTEXT__?.data);

let rootElement = document.getElementById("dot-app-root");
if (!rootElement) {
  rootElement = document.createElement("div");
  rootElement.id = "dot-app-root";
  document.body.appendChild(rootElement);
}

createRoot(rootElement).render(
  <DotRuntimeErrorBoundary>
    <DotRuntimeProvider>
      <GeneratedApp />
    </DotRuntimeProvider>
  </DotRuntimeErrorBoundary>,
);

queueMicrotask(__dotNotifyReady);
