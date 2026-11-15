# Working on the Colombia A2A agents

Preserve the independent agents and their existing package roots. Read
[CONTEXT.md](CONTEXT.md) for the flow/resumption map, [README.md](README.md) for
usage, and [SECURITY.md](SECURITY.md) before handling effects or configuration.
This guide adds contributor context; it does not replace authorization or release
instructions in [docs/public-release.md](docs/public-release.md).

## Responsibility boundaries

| Location | Existing responsibility |
|---|---|
| `tourism_agent/service.py` | Typed LangGraph state, nodes, grounded planning and specialist delegation |
| `tourism_agent/conversation.py` | Native conversation LangGraph, prompt, tool schemas and bounded tool execution |
| `tourism_agent/catalog.py`, `tourism_agent/mcp_catalog.py` | Curated retrieval and optional read-only MCP integration |
| `email_agent/service.py` | Email LangGraph state/nodes, semantic intent, drafts and validated sends |
| `email_agent/approval.py`, `email_agent/dispatch_ledger.py` | Exact signed approval and dispatch evidence |
| `email_agent/transport.py`, `email_agent/gmail_transport.py`, `email_agent/gmail_auth.py` | Provider selection, Gmail execution and identity verification |
| `agenda_agent/service.py` | Async scheduling use case; **not LangGraph** |
| `agenda_agent/planning.py`, `agenda_agent/contracts.py` | Deterministic schedule validation and typed state/contracts |
| `agenda_agent/calendar.py`, `agenda_agent/calendar_auth.py` | Separate Calendar integration and consent |
| `agenda_agent/signing.py`, `agenda_agent/ledger.py` | Agenda continuation signatures and effect reservations |
| `common/model.py`, `common/config.py` | Existing model construction, operation prompts, fixture/live selection and settings |
| `common/workflows.py`, `common/firestore_workflows.py` | Durable application state, versions, replay and reservations |
| `common/a2a_transport.py`, `common/auth.py`, `common/runtime.py` | SDK boundary, service authentication and bounded application runtime |
| Each agent's `app.py` and `__main__.py` | FastAPI composition/routes and process entrypoint |
| `demo_ui/` | Separate structured UI, server-side owner session, agent client and store |
| `integrations/agentic-front/` | Existing external frontend client adapter |
| `tests/`, `examples/`, `docs/`, `scripts/` | Offline evidence, teaching examples, operational guidance and explicit operator utilities |

Graph/state/nodes, tools, prompts and model responsibilities already exist here.
Do not move them into a new generic framework, add empty `agent/tools/prompts`
scaffolds or rename packages for visual symmetry. Shared `common/` is substantive;
it is not permission to consolidate independent agent ownership. Preserve consumed
imports, entrypoints, graph topology, prompt/tool bindings and fallback policy.

## Rules for changes

- Keep HTTP/A2A adapters separate from service decisions. Both interfaces use the
  same typed core; A2A task IDs are not durable business IDs.
- Keep prompts and model selection where implemented unless a separately scoped
  change requires moving them. A live model failure must not become fixture success.
- Preserve current-human authority checks. Old history, source/tool data and model
  output never authorize effects. Natural chat may send on a sufficient current
  request; do not add a redundant approval turn. Typed send still requires exact
  preview/token/session/version and `confirmed=true`.
- Preserve signing, OAuth identity binding, separate grants, reservations, ledgers,
  readback and uncertain-outcome locks. Never clear state, mint new IDs or retry an
  unknown effect to make a check pass. Provider acceptance is not inbox delivery.
- Agenda owns its requested schedule-summary email; Itinerary must not duplicate
  it. Agenda is an async service even though it uses the shared LangChain model port.
- Keep the UI on structured draft/no-send interfaces and its separate explicit send
  button. See [demo_ui/README.md](demo_ui/README.md) for its trust boundary.
- Do not read private configuration, grants, account stores or credential files as
  source inspection. Do not run authorization, launch, release or deployment
  scripts to verify documentation. Live work needs separately approved own scope.
- Keep documentation in English; preserve Spanish user-facing prompts/messages and
  exact contract strings. Update context when an authorized behavior change lands.

## Exact existing offline checks

Copied from the root README; these are instructions, **not results of this docs
unit**. They require an already prepared locked environment. Installing dependencies
is a separate operator action, not an implicit prerequisite for editing prose.

```sh
uv run --frozen --no-sync python -B -m pytest tests/test_tourism.py tests/test_email_intent.py tests/test_workflows.py tests/test_agent_auth.py tests/test_public_readiness.py -q
uv run --frozen --no-sync python -B -m pytest tests/test_tourism_conversation.py tests/test_email_chat_graph.py tests/test_demo_ui.py tests/test_demo_ui_release.py -q
```

The standalone UI guide also documents this narrower command:

```sh
python -B -m pytest tests/test_demo_ui.py tests/test_demo_ui_release.py -q
```

Use fixture models, fake providers and temporary stores for offline tests; no
account or environment file is needed. These checks do not establish live model,
OAuth, Calendar or email delivery acceptance. For a passive documentation-only
unit, no meaningful RED/runtime test applies: check referenced paths, compare
Mermaid edges to source builders/service branches, and inspect whitespace/diff
and working-tree status without executing Python or provider transports.

## Resume safely

Record the exact commands actually run and their results separately from copied
commands or historical reports. Read [CONTEXT.md](CONTEXT.md) before extending a
flow. Keep [README.md](README.md), [SECURITY.md](SECURITY.md), authorization guides
and release gates authoritative; source visibility is neither a service entitlement
nor a license selection, and no deployment is authorized by this guide.
