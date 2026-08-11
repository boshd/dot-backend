# Dot API

Lightweight FastAPI scaffold for Dot's shared, channel-agnostic backend.

## Run locally

```bash
uv sync
uv run uvicorn benji_api.main:app --reload
```

The API is available at `http://localhost:8000`, with interactive docs at
`http://localhost:8000/docs`.

From the parent project directory, the API can instead be built and started in the legacy-named
`benji` Compose group with `docker compose up --build -d`.

## Checks

```bash
uv run ruff check .
uv run pytest
```

## Initial contract

- `GET /health` checks service health.
- `POST /api/v1/messages` accepts a normalized message from any client or
  communications adapter. It currently returns an in-memory receipt only;
  channel adapters use the canonical conversation services below.
- `POST /api/v1/inbound/messages` accepts a message from a trusted phone-based
  communications adapter. It normalizes the sender to E.164 and atomically
  resolves or creates their user profile before returning a receipt.
- `POST /api/v1/webhooks/linq` verifies and ingests Linq Standard Webhooks. It
  deduplicates deliveries, persists channel bindings/messages, creates the
  sender's user profile, and schedules the next onboarding reply.
- `POST /api/v1/web/chat/session` opens the user's canonical direct conversation.
- `POST /api/v1/auth/eligibility` confirms that a phone number or email already belongs to a
  messaging-created Dot user before the web client starts Firebase sign-in.
- `POST /api/v1/web/chat/messages` persists a web message and synchronously returns
  the same guarded onboarding or regular agent turn used by other channels. The response includes
  `assistant_messages` for ordered text bubbles and retains `assistant_message` as a compatibility
  alias for the first bubble.
- `GET /api/v1/web/conversations` lists the user's direct and group conversations.
- `POST /api/v1/web/conversations/groups` creates a group; its invite, join, rename,
  and leave routes enforce active membership and owner permissions.
- `POST /api/v1/apps/catalog` lists the signed-in user's generated apps.
- `GET /api/v1/apps/public/{public_id}` and its record routes power bearer-link mini-apps.
- `POST /api/v1/integrations/plaid/connect` starts Plaid Link; its exchange, reconnect,
  disconnect, and verified webhook routes manage durable financial connections.

Web and first-party clients authenticate with a Firebase ID token in the `Authorization: Bearer`
header. The API verifies the token and maps a verified phone number or email to an existing Dot
identity; authentication never creates a web-only user. Configure `FIREBASE_PROJECT_ID` and either
leave `FIREBASE_SERVICE_ACCOUNT_JSON` blank for keyless verification, or provide one-line service
account JSON to enable Firebase revocation checks. Keyless verification validates the RS256
signature against Google's cached public certificates plus Firebase's issuer, audience, subject,
and timestamp claims; it cannot detect a revoked session until its ID token expires. Set
`FIREBASE_CHECK_REVOKED=true` only with service-account JSON. The web endpoints also accept a phone
number as a development-only identity selector. Set `WEB_CHAT_DEV_IDENTITY_ENABLED=false` outside
local testing.

## Conversations and channels

Each user has one canonical, long-lived direct conversation with Dot, plus any number of
shared group conversations. Linq, web,
and future iOS clients attach through channel bindings, while every message is stored
once in the shared transcript with its source channel and human sender. Group membership
has owner/member roles and active/left/removed state. Outbound transport state lives
in separate message-delivery records.

Web and first-party mobile clients read the shared transcript directly. A web turn is
not mirrored through Linq: calling Linq would send a real iMessage/RCS/SMS message and
consume transport quota. When the user returns to messaging, the agent still receives
the recent cross-channel transcript as context.

## User identity

Users have a stable UUID plus one or more normalized phone/email identifiers. Onboarding is a
generative conversation rather than a scripted sequence: a state-specific prompt
asks naturally for any missing preferred name, complete date of birth, and country.
The model returns a private structured profile proposal; application code validates
it, persists accepted fields, and derives completion. Fields can arrive in any order
or together. Date of birth is stored instead of current age so it does not become stale.

Database migrations run automatically through the one-shot `migrate` Compose
service. To run them directly:

```bash
uv run alembic upgrade head
```

## Tester resets

Reset commands are scoped to one normalized phone number or email and preview the full deletion
plan by default. From the parent project directory:

```bash
make reset-user IDENTIFIER=+15551234567
make reset-user-confirm IDENTIFIER=+15551234567

make reset-user-prod IDENTIFIER=+15551234567
make reset-user-prod-confirm IDENTIFIER=+15551234567
```

The `-confirm` targets permanently remove that identity's conversations, apps, integrations,
financial data, schedules, memories, deliveries, and webhook events. They require the identifier
again as an exact confirmation and do not affect other users.

## Linq sandbox

Copy the parent `.env.example` to `.env`, then set `LINQ_API_KEY` to the V3 token
from Linq API Tooling. Do not commit `.env`.

Expose the port 80 gateway through an HTTPS tunnel and create a Linq webhook subscription
with this target URL:

```text
https://YOUR_PUBLIC_HOST/api/v1/webhooks/linq?version=2026-02-03
```

Subscribe to message lifecycle events plus `chat.created`, `participant.added`,
`participant.removed`, `chat.group_name_updated`, and `chat.group_icon_updated`, filtered
to the Dot number. Linq returns a
`signing_secret` only once. Save it as `LINQ_WEBHOOK_SECRET` in the parent `.env`
and restart the backend:

```bash
docker compose up --build -d
```

The first direct message creates a user and starts the generative onboarding flow.
Automated replies can be disabled without stopping webhook ingestion by setting
`LINQ_AUTOMATED_REPLIES_ENABLED=false`.

## Agent system

After onboarding is complete, inbound messages are handled by the provider-neutral
agent runner. It loads recent persisted cross-channel messages as context, applies the
code-versioned Dot prompt, executes registered tools through a bounded tool loop,
persists the run and tool calls, and sends 1–4 ordered reply bubbles through Linq or web.

OpenAI's Responses API is the first model adapter. Add `OPENAI_API_KEY` to the
parent `.env` and run `make start`. The default conversation model is `gpt-5.6-terra`; change it
with `OPENAI_MODEL`. Add future providers behind the `ModelProvider` protocol and
new integrations through `ToolRegistry`.

The normal registry can enumerate connected accounts, query Calendar across linked Google
accounts, search Gmail, and read a selected result. Every query resolves active grants by the
current `user_id`; access tokens stay in the provider adapter and never enter model context.
Retrieved integration content is treated as untrusted external data.

The registry can also create a reviewed template-backed mini-app after an explicit user request.
The tool selects budget, expense-splitter, numeric-tracker, or checklist configuration and returns
an unguessable web link; it never executes model-authored application code.

Current public information is available through the provider-neutral `search_web` tool. Its first
adapter uses OpenAI Responses native web search in a separate retrieval call, then returns a
briefing and concrete source URLs to the main agent. This preserves the normal auditable tool loop
and leaves the search provider replaceable without coupling domain orchestration to OpenAI.

User messages, integration events, and due follow-ups all wake the same agent orchestration.
Integration confirmations are composed from current context instead of fixed templates. A model
may propose one later follow-up goal; application code clamps its timing, persists it durably,
cancels it when the user replies, and regenerates its wording from the latest transcript. Follow-up
wakes cannot chain. Postgres advisory locks serialize all wakes for each conversation.

## Scheduling and financial data

Durable scheduled tasks provide one clock for agent reach-outs and background capabilities. Tasks
can run once, daily, or weekly in an IANA timezone, retry with backoff, and survive restarts. A
scheduled agent wake re-enters the normal context/tool loop; short “follow up if the user stays
silent” intents remain a separate cancellable conversation behavior.

Plaid is the first adapter over provider-neutral financial connections, accounts, transactions,
and goals. Link tokens stay short-lived, access tokens are encrypted, webhook JWTs are verified,
and `/transactions/sync` cursors apply added, modified, and removed records idempotently. Dot gets
private direct-chat tools for account/cash-flow summaries, transaction search, goals, recurring
goal reviews, and explicit disconnection. Financial tools are never exposed in groups.

Configure `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`, `PLAID_COUNTRY_CODES`,
`PLAID_WEBHOOK_URL`, and—when OAuth institutions require it—an allowlisted
`PLAID_REDIRECT_URI`. Plaid does not currently provide Egyptian bank connectivity, so forwarded
bank-message and statement-import adapters should feed the same normalized tables later.

Linq typing indicators are active while an agent turn runs. Because sending clears the indicator,
direct iMessage turns restart it before every later bubble and wait a bounded, length-aware fake
typing delay. Separate bubbles remain independent idempotent deliveries. Group/SMS/RCS delivery is
paced but cannot display Linq typing dots. Tune pacing with `AGENT_INTER_BUBBLE_DELAY_SECONDS`,
`AGENT_TYPING_SECONDS_PER_CHARACTER`, and `AGENT_TYPING_MAX_DELAY_SECONDS`. Recent message context
is limited by `AGENT_CONTEXT_MESSAGE_LIMIT`. Relevant durable memories are retrieved
from the temporal Postgres/pgvector graph and injected as a separate prompt module;
completed turns enqueue consolidation for the memory worker. Incomplete users receive
the onboarding prompt module through Structured Outputs and no capability tools;
the normal tool registry becomes available on the turn after profile completion.

Group turns use a separate prompt state with member names and current-speaker identity. They do
not retrieve or consolidate personal memory, omit private profile data from model context, expose
only group-safe tools, disable follow-ups, and skip Linq typing indicators because Linq does not
support them in groups. A `participant.added` event for Dot durably targets the group and sends
one idempotent introduction. Otherwise the first discovered group message wakes Dot; later
messages wake it only in `mentions` mode when they name Dot or reply to a Dot delivery.

Memory data is separate from integration documents. Users can inspect or permanently
forget specific memories conversationally through guarded tools. See
[`../docs/memory.md`](../docs/memory.md) for the storage and processing model.

Generated apps use a versioned, composable module specification with validated module-scoped
records and a bounded custom-collection escape hatch. See
[`../docs/generated-apps.md`](../docs/generated-apps.md) for the public contract and compatibility
rules.
