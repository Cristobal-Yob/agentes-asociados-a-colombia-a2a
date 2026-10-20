# Agents associated with Colombia A2A

A classroom reference for students learning to build two independent Python agents: **Itinerary** plans a visit,
and **Email** drafts messages or executes an explicitly requested send. Each owns
its FastAPI interface, LangGraph workflow, service key and durable business state.
Itinerary can use Email through the official A2A SDK; neither needs a central
orchestrator. The same repository includes an optional Agenda agent and a local
standalone web UI prototype.

**Educational source prepared for publication in this repository.** Source
visibility does not grant access to hosted agents or authorize provider usage.
The owner has not selected an open-source license; there is no root `LICENSE`.
This repository demonstrates implementation, not a hosted service entitlement or
a claim that the local UI and every optional integration are production-validated.
See the [release and privacy notes](docs/public-release.md) and
[public privacy summary](docs/source-privacy.json).

## Student learning path

Start with deterministic tests and inspect one boundary at a time. This source
preparation does not grant students access to deployed agents. Do not distribute
institutional credentials, instructor API keys, OAuth grants or shared production
sessions. Students run the offline exercises locally; any later live exercise
requires their own separately approved accounts and configuration.

| Stage | Inspect / exercise | What to explain before moving on |
|---|---|---|
| 1. Independent agents | [Itinerary service](tourism_agent/service.py), [Email service](email_agent/service.py); run `tests/test_tourism.py` and `tests/test_email_intent.py` | Each agent accepts its own input and returns a result without requiring the other to be running |
| 2. LangGraph and LLM decisions | [Architecture node map](docs/architecture.md#native-itinerary-which-nodes-use-an-llm), [small LangGraph examples](examples/README.md); run `tests/test_tourism_conversation.py` | Distinguish a model decision from Python validation/execution; scripted tests are not a live LLM |
| 3. AgentCard and A2A | [SDK adapter](common/a2a_transport.py); run `tests/test_protocol.py` | The card describes skills/contact/auth; typed `SendMessage` delegates work and returns a task artifact |
| 4. MCP tool discovery | [Read-only catalog adapter](tourism_agent/mcp_catalog.py); run `tests/test_mcp.py` | MCP `list_tools` exposes tool schemas, not agent cards; offline tests do not contact the public catalog |
| 5. Sessions and operation IDs | [Durable workflow guide](docs/durable-workflows.md); run `tests/test_workflows.py` | Preserve request/session/version and receipts; distinguish business IDs from A2A task IDs |
| 6. Itinerary plus email draft | [Typed draft example](examples/email.json); run `tests/test_workflow_integration.py` and `tests/test_demo_ui.py` | Generate a plan, delegate a draft and inspect the result without sending mail |
| 7. Explicit send boundary | [Consent checks](tests/test_consent.py), [Email graph tests](tests/test_email_chat_graph.py) | Simulate an approved send first; a real send needs a current explicit request and independently authorized own account/configuration |

For every stage, draw the input, decision/tool, output and saved evidence. Keep
`ENABLE_REAL_EMAIL=false` and mock transport throughout offline work. Do not use
an instructor's real mailbox to verify an exercise. The optional Agenda and local
UI prototype are extensions, not prerequisites for understanding the two agents.

## Source map

| Want to inspect | Read or run |
|---|---|
| Responsibilities, LLM decisions and inputs/outputs | [Architecture](docs/architecture.md) |
| Offline behavior with no model, mail or cloud credentials | Quick path below |
| Small LangGraph client using either agent | [Mini-agent examples](examples/README.md) |
| Durable sessions, IDs and safe recovery | [Workflow guide](docs/durable-workflows.md) |
| Separate UI and its trust boundary | [UI prototype](demo_ui/README.md) |
| Disclosure and release limits | [Security policy](SECURITY.md), [public-release checklist](docs/public-release.md) |

## Quick path: deterministic and offline

Use Python 3.12 or 3.13. Dependency installation is an explicit operator action;
these commands are instructions, not evidence that a fresh installation was run.
With `uv` already installed, create the environment from the committed lock:

```sh
uv sync --frozen --all-groups --no-install-project
uv run --frozen --no-sync python -B -m pytest tests/test_tourism.py tests/test_email_intent.py tests/test_workflows.py tests/test_agent_auth.py tests/test_public_readiness.py -q
```

Tests explicitly use fixture models, fake providers and temporary stores. They do
not prove a live LLM, OAuth login, Calendar creation or delivery of an email.
For natural-language flow examples with a scripted test model:

```sh
uv run --frozen --no-sync python -B -m pytest tests/test_tourism_conversation.py tests/test_email_chat_graph.py tests/test_demo_ui.py tests/test_demo_ui_release.py -q
```

No environment file is needed for tests. Python entry points do **not** load `.env`
automatically. Unconfigured runtime and container defaults are also offline. The reference [.env.example](.env.example) and Compose now select
`MODEL_MODE=fixture`, `EMAIL_TRANSPORT=mock`, `ENABLE_REAL_EMAIL=false` and no MCP.
Compose binds only loopback ports and mounts no personal grants. It still requires
your own distinct service keys; empty keys fail closed. The legacy Windows launchers require a separate explicit live opt-in and own
configuration; they are not the offline quickstart.

The deterministic profile supports typed requests, not an imitation natural-language
router. An offline trip uses [examples/tourism.json](examples/tourism.json):

```json
{
  "message": "",
  "context": {"city": "Bogot\u00e1", "days": 2, "interests": ["culture"]},
  "draft_email": false,
  "session_id": "lesson-1"
}
```

## What the code actually implements

| Capability | Source status and limit |
|---|---|
| Itinerary and Email | Independent FastAPI services, real LangGraph workflows and typed contracts |
| Native conversation | Live LLM chooses text/tool or chat/draft/send/cancel; tests inject explicit model decisions |
| A2A | AgentCard discovery and official JSONRPC `SendMessage`, typed JSON input and task artifacts |
| Catalog | Curated records for Bogota, Medellin and Cartagena; optional bounded read-only MCP retrieval |
| Memory | SQLite per service, or explicit dedicated Firestore; bounded conversation history, versions and receipts |
| Email | Draft-only or explicit send; Gmail/optional Resend adapters, signed payloads and effect ledgers |
| Agenda | Optional third API and Calendar adapter; separate credentials and explicit event confirmation |
| Own UI | Local structured Itinerary/Email prototype, Google Web OAuth and owner-bound server session |
| Delivery automation | Separate agent and UI CI/CD sources; production readiness and live OAuth acceptance are not implied |

Catalog fixtures are not current travel research. Availability, prices, opening
hours, transport, safety and reservations are not verified. A long trip may repeat
activities. No output is a booking.

## API contract

| Interface | Itinerary | Email | Optional Agenda |
|---|---|---|---|
| Conversation / typed trip | `POST /api/chat` | `POST /api/chat` | `POST /api/chat` |
| Typed email action | Not provided | `POST /api/email` | Not provided |
| A2A | `POST /a2a` | `POST /a2a` | `POST /a2a` |
| Discovery | `GET /.well-known/agent-card.json` | Same path | Same path |
| Durable lookup | `GET /api/requests/{request_id}?session_id=...` | Same path | Same path |

Business routes require that service's `X-API-Key`. Cards, health and Swagger are
metadata, **not** a user login or proof of a private cloud service. A service key is
not a student's identity. There is no `/api/itinerary` endpoint. A2A and the REST
handlers call the same core; a LangChain tool is an adapter that can invoke A2A,
not a different protocol.

Keep the returned `request_id`, `session_id`, `plan_version` and operation receipt.
Do not create a new request to resolve an uncertain send. Query the original durable
record. A2A task/context IDs are separate and its native task cache is ephemeral.

## Drafting and sending are different

- Natural chat can request a free-topic draft without a recipient. It returns
  content, not a permission to send. A sufficient **current human** send request
  can send in the same turn; another mandatory "OK" is not required.
- Typed `POST /api/email` with `action=draft` prepares a signed preview and requires
  a recipient. `action=send` requires the exact preview, token, session/version
  and `confirmed=true`; it is not enabled by a model-generated flag.
- The UI uses typed draft/no-send contracts and a separate **Enviar este correo**
  button. It does not pass arbitrary browser messages into the native send-capable
  chat endpoint. Its Google login has not been validated live or deployed.
- Reservations precede side effects. The final Itinerary LLM receives evidence
  with tools disabled; it cannot invoke Email again. Unknown outcomes stay locked,
  rather than being retried automatically. This is not exactly-once delivery.
- `sent` plus `delivery=provider_accepted` means recorded provider acceptance,
  not delivery to the inbox or reading. `simulated_only` is only an offline result.

## Live adapters are an explicit, separate setup

Live adapters require **your own explicit configuration**, never the instructor's
account or keys. Defaults select `MODEL_MODE=fixture`, `EMAIL_TRANSPORT=mock` and
`ENABLE_REAL_EMAIL=false`. `MODEL_MODE=vertex` additionally requires an explicit
`VERTEX_PROJECT_ID`; the model validates that project before discovering ADC and
uses the same project for quota. A provider failure does not fall back to fixtures.

For Gmail, set `GMAIL_EXPECTED_EMAIL` and `GMAIL_OAUTH_PROJECT` **before starting
Python**, then authorize that exact account/project using your own Desktop client.
Set `EMAIL_TRANSPORT=gmail`, `EMAIL_SENDER` to that mailbox, a private grant path,
a strong persistent approval key, a private effect ledger and
`ENABLE_REAL_EMAIL=true` only for a separately authorized live exercise. Gmail
still verifies the selected email, verified subject, OAuth scopes and signed
payload; configuration is not permission to send. Example addresses are reserved
and cannot serve as an authorized live mailbox. Calendar needs a separate grant.

The optional UI additionally requires its own Web OAuth client, allowed Google
email, explicit agent origins, expected sender, fixed hub return and dedicated
Firestore configuration. All are server-side; missing settings fail closed. No
hosted agent origin or instructor allowlist is shipped. The hub is only entry and
return navigation, without shared cookies, tokens or a proxy.

Follow [Gmail](docs/gmail-authorization.md), [Calendar](docs/calendar-authorization.md),
[durable stores](docs/durable-workflows.md) and [CI/CD](docs/ci-cd.md) only after
separately approving live scope. Never copy keys, ADC or grants into source,
browser JavaScript, URLs, screenshots or issue reports. CD remains manual and
requires `PUBLIC_SOURCE_DEPLOY_ENABLED=true` in a protected operator environment;
a push or pull request cannot obtain cloud tokens or deploy. This publication
changes no deployed agent, key, IAM, OAuth client or cloud configuration.
