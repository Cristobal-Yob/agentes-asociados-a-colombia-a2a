# Small, real LangGraph clients

The two examples use the same independent specialist services through different
transports. They are not an offline test model or a replacement for service auth.
No example includes an API key, automatically loads a credential file or grants
access to the original deployment.

| Example | Graph and transport | When to inspect it |
|---|---|---|
| [mini_agente.py](mini_agente.py) | LLM -> one tool -> END; HTTP `POST /api/chat` | Minimal client, same common endpoint for Email or Itinerary |
| [mini_agente_correo.py](mini_agente_correo.py) | LLM decision -> optional A2A call -> text-only final response | Official SDK A2A, Email conversation and explicit durable resume IDs |

## Minimal HTTP example

`create_agent(model, url, key)` accepts your own tool-capable LangChain chat model.
The one tool forwards the current human message literally. It stores the returned
request/session/version for the next turn and does not retry a failed request.
Change **both** URL and service key to change the destination. Agent service keys
and model credentials are different trust boundaries.

The short executable `main()` requires your own explicit `VERTEX_PROJECT_ID` and
authorized ADC before live execution. It does not select an instructor project. The reusable function has
no local project imports. With an authorized configured model:

```python
from langchain_core.messages import HumanMessage
from mini_agente import create_agent

agent = create_agent(model, authorized_agent_url, server_side_service_key)
state = agent.invoke({"messages": [HumanMessage(content="Hola")]})
print(state["messages"][-1].text)
```

These names are integration inputs, not built-in variables or working credentials.
The returned tool result contains the specialist's response, not a second model
rewrite. This mini forces one specialist call each turn; the specialist itself
can still choose conversation, tools or an effect according to its contract.

## A2A example with an optional model decision

`mini_agente_correo.py` exposes `MiniEmailAgent` and a CLI. Its own model can answer
a greeting without contacting Email, or choose the adapter that discovers the
AgentCard and sends typed JSON using the official A2A SDK. Model calls in tests
are scripted; they do not consume a provider.

Inspect CLI flags without model or service calls:

```sh
python -B examples/mini_agente_correo.py --help
```

Only after separately authorizing credentials and a service destination, the CLI
can use `--backend api-key` with your own `GOOGLE_API_KEY` or `GEMINI_API_KEY` and
`EMAIL_AGENT_API_KEY` (or `--email-key-file` outside source). `--backend vertex`
requires your own explicit `VERTEX_PROJECT_ID` and authorized ADC.
Use HTTPS for nonlocal destinations. Never put credentials in a URL or source.

## Drafts, effects and recovery

A natural-language request to send may execute real email **in that turn** when the
remote service is live. Start with a draft-only request, and do not use a live
agent merely to inspect this example. Neither client disables the remote sender.
Only configured `EMAIL_TRANSPORT=mock` plus explicit offline settings guarantee
that the example recipient is not contacted. The source fixture profile supports
typed contracts; it does not fake natural conversation without an injected model.

The HTTP mini keeps its history/IDs only for that process. The remote business
store persists its request. The A2A CLI accepts `--request-id`, `--session-id` and
`--plan-version` for explicit resume. Preserve the exact returned values. A2A
transport IDs do not replace business IDs. After a lost response, query the
original protected `GET /api/requests/{request_id}?session_id=...` instead of
posting another request or generating a new operation. Unknown is not success.

For deterministic review, run `tests/test_mini_http_agent.py` and
`tests/test_mini_agent.py`; these mock the model and specialist. See the
[architecture](../docs/architecture.md) and [workflow guide](../docs/durable-workflows.md)
for authoritative service limits and effect-ledger behavior.
