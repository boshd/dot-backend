# Generated-app security boundaries

Promotion requires static policy validation, strict TypeScript checking, compilation against the
fixed dependency lock, and two independent runtime gates.

The first gate bundles a trusted LinkeDOM environment and executes it with the generated bundle in
a fresh QuickJS-WASM runtime. The guest receives a synthetic Dot bridge but no host functions,
filesystem, process, network, storage, worker, or credential access. Heap, stack, interrupt, output,
and wall-clock bounds apply. This gate proves startup and rejects runtime errors and background
mutations; it deliberately does not pretend its synthetic DOM can prove real browser behavior.

The second gate exercises declared workflows with trusted keyboard, click, submit, record-refresh,
and visible-persistence checks in a short-lived real Chromium process. Chromium's native process
sandbox must remain enabled. Page-level CSP and request interception detect network attempts, but
they are not a kernel network boundary. Production deployment must therefore run this stage in a
secret-free, network-isolated execution boundary; never solve a container namespace failure with
Chromium's `--no-sandbox` flag.

Every runtime gets a host-generated random ID namespace, and every mutation consumes a host-issued,
single-use gesture nonce during the synchronous interaction that caused it. Request and idempotency
IDs are rewritten into that namespace, so restarting a guest cannot replay deterministic SDK IDs.
Trusted bridge controls capture their JSON, collection, string, and DOM operations before generated
code starts and copy request arguments into plain bounded JSON. Prototype replacement therefore
cannot forge a passing result or a privileged request.

At delivery, the bundle digest is verified and generated JavaScript executes in an opaque-origin
iframe with scripts/forms only, a network-blocking CSP, no device permissions, and a private
MessageChannel. The parent validates every bounded operation and requires a one-use trusted gesture
for mutations. Generated code receives no provider credentials or arbitrary Dot tool access.

Residual risk remains in QuickJS-WASM, Chromium, and the generated dependency set. Before allowing
arbitrary packages, server code, uploads, or broader browser APIs, move acceptance and any server
execution into disposable microVMs or equally strong per-build sandboxes with no credentials,
network, or writable durable filesystem.
