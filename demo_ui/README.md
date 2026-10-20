# Standalone Colombia teaching workspace

This is a separate FastAPI web UI for Itinerary and Email, implemented locally.
It is not a deployed hosted-classroom entitlement or proven real Google login.
The hub is only a top-level entry and fixed return; cookies, keys and pipeline are
independent. The visible UI stays in Spanish; operational secrets stay server-side.

## Offline inspection and tests

```sh
python -B -m pytest tests/test_demo_ui.py tests/test_demo_ui_release.py -q
```

Tests inject Google, persistence and agent HTTP at their boundaries. They do not
send mail, invoke a model or prove live OAuth. `python -B -m demo_ui` can inspect
the unconfigured loopback page; without every required setting, login and API
execution fail closed. There is no development login bypass or fake production
session. No actual hosted agent URL or instructor account is selected by default.

## Workflow

```text
own HTTPS UI -> Google Web OAuth -> configured-owner session
   |
   +-> structured trip -> Itinerary typed /api/chat, message=""
   |                                      +-> optional A2A email DRAFT
   +-> separate draft -> Email /api/email action=draft
                                          |
                   server stores exact signed preview and correlation
                                          |
                       explicit "Enviar este correo" button
                                          |
                      atomic reservation -> exact typed send
                                          |
                        receipt or locked uncertain outcome
```

Browser fields cannot select arbitrary upstream actions, URLs, IDs, confirmation
flags, signatures or contexts. This intentionally uses structured draft/no-send
contracts, not unrestricted native chat: natural-language chat can send immediately
when the current human explicitly requests it. A prompt saying "do not send" is not
an authorization boundary.

State, nonce and PKCE bind login to one browser and one use. Verified Google `sub`
owns the server workspace; the configured email allowlist restricts who may log in.
Sessions are opaque, hashed, expiring, host-only and Secure/HttpOnly/SameSite=Lax
on HTTPS. Mutations also require same Origin, JSON and CSRF. Logout revokes the
session. Tokens, grant secrets, keys and signed previews never enter browser storage.
Callback access logs are disabled; external logging must also exclude auth queries.

Sending uses the exact server-held preview/token/session/version and a separately
persisted operation ID. A transactional reservation precedes HTTP, so repeated
clicks do not dispatch again. Changed recipient/content needs a new signed draft.
A preview expires after five minutes. Unknown outcomes remain locked; reconciliation
only reads the matching durable upstream operation. There is no automatic resend
or unlock endpoint. `provider_accepted` is acceptance, not inbox delivery or reading.

## Explicit own configuration

The app does not read `.env.example` automatically. Configure the process before
startup, with secrets via protected server-side bindings, never frontend variables.

| Setting | Required value |
|---|---|
| `COLOMBIA_UI_ORIGIN` | Verified own HTTPS origin; fixed loopback HTTP only for local inspection |
| `COLOMBIA_UI_GOOGLE_CLIENT_ID`, `COLOMBIA_UI_GOOGLE_CLIENT_SECRET` | Dedicated own Web OAuth client, not a Desktop Gmail client |
| `COLOMBIA_UI_ALLOWED_EMAIL` | Exact lower-case Google owner allowed to log in |
| `COLOMBIA_UI_ITINERARY_URL`, `COLOMBIA_UI_EMAIL_URL` | Fixed approved distinct service origins, never browser input |
| `COLOMBIA_UI_HUB_RETURN` | Fixed approved HTTPS return URL, no user-controlled return parameter |
| `COLOMBIA_UI_SENDER_EMAIL` | Exact independently authorized mail sender; mismatching previews are rejected |
| `COLOMBIA_UI_FIRESTORE_PROJECT`, `COLOMBIA_UI_FIRESTORE_DATABASE` | Own project and dedicated nondefault UI database, not an agent's database |
| `COLOMBIA_UI_ITINERARY_KEY`, `COLOMBIA_UI_EMAIL_KEY` | Distinct server-only service credentials |

OAuth callback is `{COLOMBIA_UI_ORIGIN}/auth/callback`, exact match. Scopes are
`openid email`; no Gmail permissions or offline refresh grant. Configure own
resources independently; source publication creates none and enrolls no student.

Firestore uses `colombia_ui_v1`. The application checks expiry synchronously;
provisioning a TTL policy/retention and least-privilege access is separate operator
work. Workspace state is bounded (one pending operation, ten new requests/minute,
six recent operations); it is not an unlimited history service.

## Independent CI/CD and acceptance

UI workflows remain separate from agents and the hub. CI checks only this package
with pinned requirements and hermetic tests. CD remains manual, protected and
requires `PUBLIC_SOURCE_DEPLOY_ENABLED=true`, explicitly approved own inputs,
reviewed main SHA and Python image digest. It stages only the runtime allowlist,
requires an already provisioned service and creates a digest-pinned revision with
no traffic. No push, source visibility or fixture receipt authorizes deployment.

Before any real publication as a runnable service, independently test Google login,
anonymous denial, owner access, draft-only behavior, private upstream calls and
rollback. Any real send requires separate explicit authorization. These live checks
have not been established by this source candidate. No IAP implementation is claimed.
