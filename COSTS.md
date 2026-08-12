# Dot cost ledger

_Last reviewed: August 12, 2026_

This is the source of truth for Dot's production operating costs. Update the current-period
snapshot weekly during the MVP and before enabling any paid provider or materially changing
models, replicas, retention, regions, or quotas. Never record credentials here.

## MVP budget policy

- Keep known infrastructure and API spend below **$30/month** while Dot has fewer than 100 users.
- Keep Firebase on the free Spark plan until more than 10 phone verifications/day are necessary.
- Prefer passwordless email links for repeat testing; use Firebase test numbers for scripted demos.
- Keep Plaid in Sandbox/Trial and Linq in Sandbox until their production prices are recorded below.
- Review costs weekly and after every demo spike. Record invoice totals monthly.

## Current period

Billing period: **August 11–September 11, 2026**.

| Provider | Actual/estimate | Guardrail | Status |
| --- | ---: | --- | --- |
| Railway | $0.01 used; $0.85 current metered projection; $5 Hobby minimum | $10 soft alert; $20 hard stop | Active |
| Firebase Auth | $0 | Spark: hard 10 sent SMS/day; email links enabled | Active, no billing account |
| OpenAI | Review dashboard | Set a dedicated Dot project alert and hard limit before external beta; track app-build cost separately | Active |
| Linq | $0 sandbox | 100 combined messages/day in sandbox | Sandbox; production quote TBD |
| Plaid | $0 sandbox/trial | Do not accept production pricing without updating this ledger | Sandbox/trial |
| Google Calendar/Gmail | $0 expected | On-demand queries; monitor quota | Active |
| Google Pub/Sub | $0 expected | First 10 GiB/month free | Active |
| Domain | Unrecorded | Add the Spaceship invoice amount | `textdot.co` purchased; invoice pending in ledger |

Run `make costs` for the latest Railway usage and project breakdown.

## Provider pricing and controls

| Provider | Cost model | MVP control | Official source |
| --- | --- | --- | --- |
| Railway | Hobby is at least $5/month including $5 usage; then metered RAM, CPU, egress, and volume | One replica per service, private networking, $10 soft/$20 hard workspace limit; QuickJS plus one short-lived Chromium process run only while the dedicated app builder promotes a revision. Page interception detects network attempts; deploy the browser stage in a secret-free network-isolated runner before public app building. Watch builder CPU/RAM after beta app-build volume begins | [Pricing](https://docs.railway.com/pricing/plans), [cost controls](https://docs.railway.com/pricing/cost-control) |
| Firebase / Identity Platform | First 50,000 MAU free. Spark currently caps sent verification SMS at 10/day. If Blaze is enabled, the first 10 SMS/day remain free; later sends are destination-priced | Stay on Spark initially; explicit region allowlist; eligibility throttling and reCAPTCHA | [Pricing](https://cloud.google.com/identity-platform/pricing), [quotas](https://docs.cloud.google.com/identity-platform/quotas), [SMS regions](https://docs.cloud.google.com/identity-platform/docs/admin/sms-regions) |
| OpenAI | Terra: $2/M input, $0.20/M cached input, and $12/M output. Luna: $0.20/M input, $0.02/M cached input, and $1.20/M output. Embeddings: $0.02/M tokens. Web search: $0.01/call plus tokens | Dedicated project limits; low reasoning for normal work; app builds default to Terra with bounded repair, at most three concurrent builds and 20 active code apps per user, and recorded tokens/latency/repair count per build. A/B Luna on the same eval set before routing simple builds there; it is 10x cheaper at equal token counts. Codex is not assumed cheaper and Dot already owns the compiler/test/repair harness. Inbound media uses low-detail images, 20 MB/item, 45 MB/request, and eight remote items/request | [Pricing](https://developers.openai.com/api/docs/pricing), [code generation](https://developers.openai.com/api/docs/guides/code-generation), [spend limits](https://developers.openai.com/api/docs/guides/spend-limits) |
| Linq | Sandbox free for 7 days with 100 combined messages/day; production pricing is private | Keep sandbox until quote is recorded; monitor outbound bubbles, retries, and proactive sends | [Pricing](https://linqapp.com/s/pricing), [limits](https://docs.linqapp.com/guides/platform/rate-limits/) |
| Plaid | Sandbox free; Trial includes 10 live Items. Transactions is billed monthly per connected Item in production; public unit price unavailable | Stay in Sandbox/Trial; remove unused Items; do not enable separately priced Balance/Refresh silently | [Billing](https://plaid.com/docs/account/billing/) |
| Google Workspace APIs | Calendar/Gmail API usage has no additional charge at current standard quotas | On-demand queries rather than full indexing; watch 2026 pricing changes | [Calendar quota](https://developers.google.com/workspace/calendar/api/guides/quota), [Gmail quota](https://developers.google.com/workspace/gmail/api/reference/quota) |
| Google Pub/Sub | First 10 GiB/month free; then usage/retention pricing | Minimal topic retention and one production push subscription | [Pricing](https://cloud.google.com/pubsub/pricing) |

Generated-app promotion now starts one real Chromium process after the fast QuickJS gate. A
representative local primary-form run measured 2.442 seconds for Chromium (3.070 seconds including
compilation) and 272,138,240 bytes maximum resident memory for the full test process tree. The
builder remains serial and Chromium exits after every candidate, so idle Railway usage is unchanged;
re-measure on Railway before increasing build concurrency.

### Firebase SMS exposure if Blaze is enabled later

Prices apply after the first 10 sent SMS per project per day. Failed delivery can still count as
sent usage.

| Destination | Unit price | 100 sends in one day |
| --- | ---: | ---: |
| Egypt | $0.12 | $10.80 billable after the free 10 |
| United States | $0.01 | $0.90 |
| Canada | $0.01 | $0.90 |
| United Kingdom | $0.05 | $4.50 |
| Germany | $0.09 | $8.10 |

## Monthly snapshots

| Month | Railway | Firebase/GCP | OpenAI | Linq | Plaid | Other | Total | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-08 | Pending | $0 | Pending | $0 | $0 | $0 | Pending | Production setup began August 11 |
