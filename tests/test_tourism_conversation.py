import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_email_chat_graph import SemanticModel
from test_workflow_integration import Transport

from common.a2a_transport import send_typed
from common.config import Settings
from common.contracts import EmailRequest, EmailResponse, TourismRequest
from email_agent.app import create_app as email_app
from tourism_agent.conversation import valid_arguments
from tourism_agent.service import TourismService


def action(name, args=None, identifier="tool-1"):
    return {"name": name, "args": args or {}, "id": identifier, "type": "tool_call"}


def plan_call():
    return action("generar_itinerario", {"city": "Bogota", "days": 1, "interests": ["culture"]})


class Conversation:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []
        self.text_calls = 0

    async def tourism_turn(self, messages, tools):
        self.messages.append(deepcopy(messages))
        assert self.responses, "El bucle nativo excedió las respuestas previstas."
        return self.responses.pop(0)

    async def tourism_text(self, messages, tools):
        self.text_calls += 1
        response = await self.tourism_turn(messages, tools)
        if isinstance(response, BaseException):
            raise response
        return response


class Planner:
    def __init__(self):
        self.calls = 0

    async def invoke(self, payload):
        self.calls += 1
        return {"ids": [payload["sources"][0]["id"]], "email_action": "none"}


@pytest.fixture
def system(tmp_path):
    transport = Transport()
    email = email_app(
        Settings.offline(
            approval_secret="k" * 40,
            workflow_db_path=str(tmp_path / "email.sqlite"),
            public_url="http://127.0.0.1:8002",
        ),
    )
    email.state.service.transport = transport
    planner, requests = Planner(), []
    peer_mode = {"semantic": False, "send": False}
    settings = Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite"))

    async def peer(request):
        requests.append(request)
        wire = request
        if not peer_mode["semantic"]:
            # Las pruebas de compatibilidad usan acciones tipadas explícitas, no
            # un intérprete de mensajes. Los casos semánticos inyectan su modelo.
            wire = request.model_copy(
                update={
                    "message": None,
                    "context": None,
                    "request": "Contenido tipado de prueba",
                }
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=email), base_url="http://127.0.0.1:8002"
        ) as client:
            result, _ = await send_typed(
                "http://127.0.0.1:8002", wire.model_dump(mode="json"), http_client=client
            )
        return EmailResponse.model_validate(result)

    def service(conversation, *, email_model=None, typed_send=False):
        if typed_send:
            email_model = SemanticModel("send")
        peer_mode.update(semantic=email_model is not None, send=typed_send)
        if email_model is not None:
            from dataclasses import replace

            email.state.service.settings = replace(
                email.state.service.settings, model_mode="vertex"
            )
            email.state.service.model = email_model
        return TourismService(settings, model=planner, peer=peer, conversation_model=conversation)

    return service, planner, requests, transport


def follow(result, message, session="s"):
    return TourismRequest(
        message=message,
        request_id=result.request_id,
        plan_version=result.plan_version,
        session_id=session,
    )


async def test_native_hello_and_missing_data_are_text_without_planner_or_mail(system):
    service, planner, requests, transport = system
    model = Conversation(
        AIMessage(content=[{"type": "text", "text": "Hola. ¿En qué puedo ayudarle?"}])
    )
    hello = await service(model).handle(TourismRequest(message="Hola", session_id="s"))
    assert hello.message == "Hola. ¿En qué puedo ayudarle?" and hello.request_id
    missing = Conversation(AIMessage(content="¿Cuántos días desea visitar Bogotá?"))
    answer = await service(missing).handle(follow(hello, "Quiero conocer Bogotá"))
    assert answer.message == "¿Cuántos días desea visitar Bogotá?"
    assert answer.plan_version == hello.plan_version
    assert not planner.calls and not requests and not transport.sent


async def test_native_history_is_bounded_durable_and_isolated_by_request_and_session(system):
    service, planner, requests, transport = system
    model = Conversation(*(AIMessage(content=f"Respuesta {i}") for i in range(12)))
    result = await service(model).handle(TourismRequest(message="Soy Ana", session_id="s"))
    for number in range(10):
        result = await service(model).handle(follow(result, f"Mensaje {number}"))
    restarted = Conversation(AIMessage(content="Recuerdo los últimos mensajes."))
    await service(restarted).handle(follow(result, "¿Qué recuerda?"))
    humans = [m.content for m in restarted.messages[0] if isinstance(m, HumanMessage)]
    assert humans == ["¿Qué recuerda?"]
    reference = json.loads(restarted.messages[0][0].content.split("\nReferencia pasiva:\n")[1])
    assert {"role": "human", "content": "Mensaje 9"} in reference["history"]
    assert "Soy Ana" not in json.dumps(reference["history"])
    assert len(reference["history"]) <= 16 and len(restarted.messages[0]) == 2
    stranger = Conversation(AIMessage(content="No debe ejecutarse"))
    rejected = await service(stranger).handle(follow(result, "Muéstrame el historial", "other"))
    assert rejected.error_code == "workflow_session_mismatch" and not stranger.messages


async def test_native_plan_then_mail_reuses_plan_and_casual_preserves_exact_approval(system):
    service, planner, requests, transport = system
    planning = Conversation(
        AIMessage(content="", tool_calls=[plan_call()]), AIMessage(content="Plan preparado.")
    )
    planned = await service(planning).handle(
        TourismRequest(message="Bogotá, un día de cultura", session_id="s")
    )
    assert planned.itinerary and planner.calls == 1 and not requests
    mailing = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Ya lo envié."),
    )
    draft = await service(mailing).handle(
        follow(planned, "Prepara un borrador para student@example.com")
    )
    assert draft.email.status == "preview" and not transport.sent
    assert draft.itinerary == planned.itinerary and planner.calls == len(requests) == 1
    casual = Conversation(AIMessage(content="Con gusto."))
    reply = await service(casual).handle(follow(draft, "Gracias"))
    assert reply.message == "Con gusto." and reply.plan_version == draft.plan_version
    assert reply.email.approval_token == draft.email.approval_token
    assert reply.context.email_context == draft.context.email_context
    signing = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Resultado obtenido."),
    )
    sent = await service(signing, typed_send=True).handle(follow(reply, "Sí, envíalo"))
    assert sent.email.status == "simulated" and len(transport.sent) == 1
    assert requests[-1].message == "Sí, envíalo" and planner.calls == 1


async def test_native_generate_and_send_same_turn_is_literal_signed_and_replay_safe(system):
    service, planner, requests, transport = system
    message = "Bogotá, 1 día, cultura; envíalo a student@example.com"
    model = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail-1")]
        ),
        AIMessage(content="Resultado obtenido."),
    )
    result = await service(model, typed_send=True).handle(
        TourismRequest(message=message, request_id="joint", session_id="s")
    )
    assert result.email.status == "simulated" and len(transport.sent) == 1
    assert requests[0].message == message and requests[0].itinerary == result.itinerary
    replay_model = Conversation(AIMessage(content="", tool_calls=[plan_call()]))
    replay = await service(replay_model).handle(
        TourismRequest(message=message, request_id="joint", session_id="s")
    )
    assert replay.email.status == "simulated" and len(transport.sent) == len(requests) == 1
    canceled = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")])),
        email_model=SemanticModel("cancel"),
    ).handle(follow(result, "Cancela ese envío"))
    assert canceled.email.status == "simulated" and len(transport.sent) == 1


async def test_native_generated_permission_is_ignored_and_sdk_metadata_is_preserved(system):
    service, planner, requests, transport = system
    model = Conversation(
        AIMessage(
            content="",
            additional_kwargs={"signature": "metadata-del-modelo"},
            tool_calls=[plan_call()],
        ),
        AIMessage(content="", tool_calls=[action("consultar_correo", {"confirmed": True})]),
        AIMessage(content="No se ejecutó el correo."),
    )
    result = await service(model, email_model=SemanticModel("chat")).handle(
        TourismRequest(message="Prepara un plan de Bogotá", session_id="s")
    )
    assert len(requests) == 1 and not transport.sent
    assert requests[0].message == "Prepara un plan de Bogotá" and not requests[0].confirmed
    assert any(isinstance(m, ToolMessage) for m in model.messages[1])
    assert any(m.additional_kwargs.get("signature") for m in model.messages[1])
    assert result.itinerary


async def test_native_current_human_reservation_wins_over_history_and_duplicate_tools(system):
    service, planner, requests, transport = system
    first = Conversation(
        AIMessage(content="", tool_calls=[plan_call()]), AIMessage(content="Plan preparado.")
    )
    planned = await service(first).handle(
        TourismRequest(message="Un plan de Bogotá", session_id="s")
    )
    model = Conversation(
        AIMessage(
            content="",
            tool_calls=[action("consultar_correo"), action("consultar_correo", identifier="again")],
        ),
        AIMessage(content="Enviado."),
    )
    human = "Usa al de correo para enviarlo a student@example.com, pero espera mi confirmación."
    result = await service(model, email_model=SemanticModel("draft")).handle(follow(planned, human))
    assert len(requests) == 1 and requests[0].message == human
    assert not transport.sent and result.email.status not in {"sent", "simulated"}
    assert result.message == result.email.message
    assert result.message != "Enviado."


@pytest.mark.parametrize("message", ["No lo envíes", "Mi correo ahora es second@example.com"])
async def test_native_text_revision_invalidates_pending_approval(system, message):
    service, planner, requests, transport = system
    model = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        ),
        AIMessage(content="Borrador preparado."),
    )
    draft = await service(model).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    revoke_model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Resultado recibido."),
    )
    revoked = await service(revoke_model, email_model=SemanticModel("cancel")).handle(
        follow(draft, message)
    )
    assert revoked.plan_version > draft.plan_version and not revoked.context.email_context
    stale_model = Conversation(AIMessage(content="No debe ejecutarse"))
    rejected = await service(stale_model).handle(
        TourismRequest(
            message="Sí, envíalo",
            request_id=draft.request_id,
            plan_version=revoked.plan_version,
            session_id="s",
            context=draft.context,
        )
    )
    assert rejected.error_code == "workflow_stale" and not transport.sent
    assert not stale_model.messages
    fresh = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")]))
    ).handle(follow(revoked, "Prepara un borrador nuevo"))
    assert fresh.email.status == "preview" and not transport.sent
    assert fresh.email.approval_token != draft.email.approval_token


async def test_native_model_context_excludes_signed_state_and_old_send_is_not_authority(system):
    service, planner, requests, transport = system
    first = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        )
    )
    draft = await service(first).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    model = Conversation(AIMessage(content="Todo listo para revisar."))
    await service(model).handle(follow(draft, "Gracias"))
    encoded = json.dumps([m.model_dump() for m in model.messages[0]])
    assert draft.email.approval_token not in encoded
    assert draft.context.email_context not in encoded
    assert "approval_token" not in encoded and "email_context" not in encoded
    other = Conversation(AIMessage(content="Otra conversación."))
    await service(other).handle(TourismRequest(message="Hola", session_id="other"))
    assert "student@example.com" not in json.dumps([m.model_dump() for m in other.messages[0]])


@pytest.mark.parametrize("order", ["envíalo", "super mandamelo por correo pls"])
async def test_native_unknown_peer_result_blocks_hello_and_replay_without_retry(system, order):
    service, planner, requests, transport = system
    model = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        )
    )
    agent = service(model)
    calls = []

    async def uncertain(request):
        calls.append(request)
        raise TimeoutError

    agent.peer = uncertain
    result = await agent.handle(
        TourismRequest(
            message=f"Bogotá, 1 día, cultura; {order} a student@example.com", session_id="s"
        )
    )
    assert result.workflow_state == "unknown"
    restarted_model = Conversation(AIMessage(content="No debe ejecutarse"))
    replay = await service(restarted_model).handle(follow(result, "Hola"))
    assert replay.workflow_state == "unknown" and len(calls) == 1
    assert not restarted_model.messages and not transport.sent


async def test_native_semantic_mail_uses_current_plan_and_literal_over_real_a2a(system):
    service, planner, requests, transport = system
    planning = Conversation(
        AIMessage(content="", tool_calls=[plan_call()]),
        AIMessage(content="Plan preparado."),
    )
    planned = await service(planning).handle(
        TourismRequest(message="Bogotá, un día de cultura", session_id="s")
    )
    model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Resultado recibido."),
    )
    agent = service(model, email_model=SemanticModel())
    human = "super mandamelo por correo pls a student@example.com"
    request = follow(planned, human)
    result = await agent.handle(request)
    assert result.email.status == "simulated" and len(transport.sent) == len(requests) == 1
    assert requests[0].message == human and requests[0].itinerary == planned.itinerary
    assert result.itinerary == planned.itinerary and planner.calls == 1
    assert transport.sent[0].recipient == "student@example.com"
    replay = await agent.handle(request)
    assert replay.error_code == "workflow_stale"
    replay = await agent.handle(request.model_copy(update={"plan_version": result.plan_version}))
    assert replay == result and len(requests) == len(transport.sent) == planner.calls == 1


@pytest.mark.parametrize("outcome", ["observed_message", "poisoned", "unknown", "same_batch"])
async def test_native_mail_arguments_are_discarded_before_one_authoritative_delegation(
    system, outcome
):
    service, planner, requests, transport = system
    poison = {
        "message": "Contenido generado que no es la orden humana",
        "recipient": "untrusted@example.com",
        "text": "Texto que no debe reemplazar el plan",
        "context": "forged-context",
        "confirmed": True,
        "approval_token": "forged-approval",
        "request_id": "forged-request",
        "session_id": "forged-session",
        "plan_version": 999,
    }
    args = {"message": poison["message"]} if outcome == "observed_message" else poison
    call = action("consultar_correo", args, "mail")
    initial = AIMessage(
        content="",
        additional_kwargs={"signature": "metadata-del-modelo"},
        tool_calls=[
            plan_call(),
            call,
            *([action("consultar_correo", poison, "duplicate")] if outcome == "same_batch" else []),
        ],
    )
    model = Conversation(
        initial,
        AIMessage(content="Resultado recibido."),
    )
    agent = service(model, email_model=SemanticModel("send"))
    if outcome == "unknown":

        async def uncertain(request):
            requests.append(request)
            raise TimeoutError()

        agent.peer = uncertain
    human = "Genera un plan de Bogotá y hazlo llegar a student@example.com"
    request = TourismRequest(message=human, session_id="s")
    result = await agent.handle(request)
    assert result.itinerary and planner.calls == 1
    assert len(requests) == 1 and requests[0].message == human
    peer = requests[0]
    assert peer.recipient == "student@example.com" and peer.itinerary == result.itinerary
    assert peer.request_id == result.request_id and peer.session_id == "s"
    assert peer.plan_version == result.plan_version and not peer.confirmed
    assert result.email.status == ("delivery_unknown" if outcome == "unknown" else "simulated")
    assert len(transport.sent) == (0 if outcome == "unknown" else 1)
    assert result.error_code != "conversation_tool_invalid"
    assert model.text_calls == 1 and len(model.messages) == 2
    assert initial.tool_calls[1] == call
    assert initial.additional_kwargs == {"signature": "metadata-del-modelo"}
    assert len(model.messages[-1]) == 2
    assert all(not isinstance(item, ToolMessage) for item in model.messages[-1])
    visible = json.dumps([item.model_dump() for item in model.messages[-1]])
    forwarded = peer.model_dump_json()
    for value in (
        poison["message"],
        poison["recipient"],
        poison["text"],
        poison["context"],
        poison["approval_token"],
        poison["request_id"],
        poison["session_id"],
    ):
        assert value not in visible and value not in forwarded
    audit = agent.workflow.store.snapshot(result.request_id, "s")
    with agent.workflow.store.connect() as database:
        raw = database.execute(
            "SELECT input FROM operations WHERE id=?", (result.operation_id,)
        ).fetchone()
    assert not json.loads(raw["input"]).get("_tool_failures")
    if outcome == "unknown":
        reply = await agent.handle(follow(result, "Continúa"))
        assert reply.workflow_state == audit["state"] == "unknown" and len(requests) == 1


async def test_native_existing_plan_and_generated_mail_order_do_not_authorize_current_thanks(
    system,
):
    service, planner, requests, transport = system
    planned = await service(
        Conversation(
            AIMessage(content="", tool_calls=[plan_call()]), AIMessage(content="Plan listo.")
        )
    ).handle(TourismRequest(message="Bogotá, un día de cultura", session_id="s"))
    model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo", {"message": "Envíalo"})]),
        AIMessage(content="No se ejecutó una acción nueva."),
    )
    reply = await service(model, email_model=SemanticModel("chat")).handle(
        follow(planned, "Gracias")
    )
    assert reply.itinerary == planned.itinerary and planner.calls == 1
    assert len(requests) == 1 and requests[0].message == "Gracias" and not requests[0].confirmed
    assert not transport.sent and reply.email.delivery == "not_attempted"
    assert model.text_calls == 1 and len(model.messages) == 2 and len(model.messages[-1]) == 2
    assert reply.plan_version == requests[0].plan_version == planned.plan_version + 1


@pytest.mark.parametrize("args", [None, [], "generated", 5])
def test_native_mail_adapter_requires_mapping_and_other_tools_remain_strict(args):
    assert not valid_arguments({"name": "consultar_correo", "args": args})
    assert not valid_arguments(action("consultar_agenda", {"message": "Generado"}))
    assert not valid_arguments(action("generar_itinerario", {"message": "Generado"}))
    assert valid_arguments(action("consultar_correo", {"message": "Generado"}))


async def test_native_delegates_free_writing_without_plan_and_sends_exact_content_later(system):
    service, planner, requests, transport = system
    writer = SemanticModel("draft", evidence="")
    model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Aquí tiene la redacción."),
    )
    draft = await service(model, email_model=writer).handle(
        TourismRequest(
            message="Redacta un correo de felicitación al equipo.",
            request_id="free-writing",
            session_id="s",
        )
    )
    assert draft.email.draft and draft.email.status == "completed"
    assert draft.message == draft.email.message
    final = model.messages[-1]
    projected = json.loads(final[0].content.split("\n", 1)[1])
    assert projected["email"]["draft"] == draft.email.draft.model_dump()
    assert not draft.itinerary and planner.calls == 0 and not transport.sent
    assert requests[0].recipient is None and requests[0].itinerary is None
    assert not draft.email.approval_token and not draft.email.preview
    sender = SemanticModel("send", reuse_draft=True)
    sending = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Resultado recibido."),
    )
    result = await service(sending, email_model=sender).handle(
        follow(draft, "Envíalo a student@example.com")
    )
    assert result.email.status == "simulated" and len(transport.sent) == 1
    assert (transport.sent[0].subject, transport.sent[0].text) == (
        draft.email.draft.subject,
        draft.email.draft.text,
    )
    assert len(requests) == 2 and planner.calls == 0
    assert [call["operation"] for call in sender.calls] == ["email_turn"]


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("decision", ["draft", "cancel", "provider"])
async def test_native_compound_no_send_request_writes_and_revokes_before_delegation(
    system, pending, decision
):
    service, planner, requests, transport = system
    previous = None
    if pending:
        previous = await service(
            Conversation(
                AIMessage(
                    content="",
                    tool_calls=[plan_call(), action("consultar_correo", identifier="mail")],
                )
            )
        ).handle(
            TourismRequest(
                message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
                session_id="s",
            )
        )
        assert previous.email.preview
    before = len(requests)
    human = (
        "Redacta un correo profesional para pedir reagendar una clase por un imprevisto. "
        "Deja el nombre y la fecha como marcadores; muestra asunto y texto, no lo envíes."
    )
    model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="Resultado recibido."),
    )
    agent = service(
        model,
        email_model=SemanticModel(
            decision,
            evidence="" if decision == "draft" else human,
            content_source="message",
        ),
    )
    if decision == "provider":

        async def unavailable(messages, tools):
            raise TimeoutError("Proveedor sintético no disponible")

        model.tourism_turn = unavailable
    request = follow(previous, human) if previous else TourismRequest(message=human, session_id="s")
    result = await agent.handle(request)
    assert not transport.sent
    if decision == "provider":
        assert result.error_code == "conversation_model_error" and len(requests) == before
    else:
        assert result.email and len(requests) == before + 1
        assert requests[-1].message == human
        assert result.email.delivery == "not_attempted"
    if decision == "draft":
        assert result.email.draft and "reagendar una clase" in result.email.draft.text
        assert result.email.preview is result.email.approval_token is None
    elif decision == "cancel":
        assert result.email.status == "cancelled" and result.email.preview is None
    snapshot = agent.workflow.store.snapshot(result.request_id, "s")
    if decision != "provider":
        assert snapshot["approval"] is None and not result.context.email_context
    if previous and decision != "provider":
        stale = await agent.handle(
            follow(result, "Sí, envíalo").model_copy(update={"context": previous.context})
        )
        assert stale.error_code == "workflow_stale" and not transport.sent


async def test_native_cas_rejects_send_decision_if_text_revocation_wins_during_model_call(system):
    service, planner, requests, transport = system
    first = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        )
    )
    draft = await service(first).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    entered, released = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def tourism_turn(self, messages, tools):
            entered.set()
            await released.wait()
            return AIMessage(content="", tool_calls=[action("consultar_correo")])

    pending = asyncio.create_task(
        service(Delayed(), typed_send=True).handle(follow(draft, "Sí, envíalo"))
    )
    await entered.wait()
    revoked = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")])),
        email_model=SemanticModel("cancel"),
    ).handle(follow(draft, "Cancela ese envío"))
    released.set()
    result = await pending
    assert revoked.plan_version > draft.plan_version
    assert result.error_code == "workflow_stale" and not transport.sent
    assert len(requests) == 2


async def test_native_known_structured_recipient_does_not_need_human_text_rewriting(system):
    service, planner, requests, transport = system
    first = await service(Conversation(AIMessage(content="Conservaré ese destinatario."))).handle(
        TourismRequest(
            message="Ese es mi destinatario", recipient="student@example.com", session_id="s"
        )
    )
    model = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        )
    )
    human = "Bogotá, 1 día, cultura; envía el itinerario"
    result = await service(model, typed_send=True).handle(follow(first, human))
    assert result.email.status == "simulated" and len(transport.sent) == 1
    assert requests[0].message == human and transport.sent[0].recipient == "student@example.com"


async def test_native_changed_recipient_confirmation_revokes_before_model_or_effect(system):
    service, planner, requests, transport = system
    draft = await service(
        Conversation(
            AIMessage(
                content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
            )
        )
    ).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    model = Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")]))
    changed = await service(model, typed_send=True).handle(
        follow(draft, "Sí, envíalo").model_copy(update={"recipient": "second@example.com"})
    )
    assert not transport.sent and len(requests) == 2
    assert model.messages
    assert changed.plan_version > draft.plan_version
    assert changed.context.recipient == "second@example.com"
    assert not changed.context.email_context
    assert changed.itinerary == draft.itinerary
    snapshot = service(model).workflow.store.snapshot(changed.request_id, "s")
    assert snapshot["approval"] is None
    stale = await service(model).handle(
        follow(changed, "Sí, envíalo").model_copy(update={"context": draft.context})
    )
    assert stale.error_code == "workflow_stale" and not transport.sent
    fresh = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")]))
    ).handle(follow(changed, "Prepara un borrador nuevo"))
    assert fresh.email.status == "preview" and not transport.sent
    assert fresh.email.preview.recipient == "second@example.com"
    assert fresh.email.approval_token != draft.email.approval_token
    confirmation = follow(fresh, "Sí, envíalo")
    sent = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")])),
        typed_send=True,
    ).handle(confirmation)
    assert sent.email.status == "simulated" and len(transport.sent) == 1
    assert transport.sent[0].recipient == requests[-1].recipient == "second@example.com"
    replay = await service(Conversation()).handle(
        confirmation.model_copy(update={"plan_version": sent.plan_version})
    )
    assert replay.email.status == "simulated" and len(transport.sent) == 1
    assert planner.calls == 1


async def test_native_changed_recipient_with_tool_discards_old_signed_continuation(system):
    service, planner, requests, transport = system
    draft = await service(
        Conversation(
            AIMessage(
                content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
            )
        )
    ).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    fresh = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")]))
    ).handle(
        follow(draft, "Prepara un borrador nuevo").model_copy(
            update={"recipient": "second@example.com"}
        )
    )
    assert not transport.sent and fresh.email.status == "preview"
    assert fresh.email.preview.recipient == "second@example.com"
    assert requests[-1].context is None and requests[-1].recipient == "second@example.com"
    assert fresh.plan_version > draft.plan_version and planner.calls == 1


async def test_native_email_peer_binds_current_recipient_to_signed_preview(system):
    service, planner, requests, transport = system
    draft = await service(
        Conversation(
            AIMessage(
                content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
            )
        )
    ).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    result = await service(Conversation(), typed_send=True).peer(
        EmailRequest(
            message="Sí, envíalo",
            request_id=draft.request_id,
            plan_version=draft.plan_version,
            session_id="s",
            context=draft.context.email_context,
            recipient="second@example.com",
        )
    )
    assert not transport.sent and result.error_code == "recipient_mismatch"


@pytest.mark.parametrize(
    "phase",
    [
        "arguments",
        "catalog",
        "select_invocation",
        "select_shape",
        "grounded_ids",
        "itinerary_construction",
    ],
)
async def test_native_tool_failure_records_private_sanitized_cause(system, monkeypatch, phase):
    service, planner, requests, transport = system
    call = plan_call()
    private_marker = "private-value-never-recorded"
    if phase == "arguments":
        call["args"].update(days="one", **{private_marker: private_marker})
    elif phase == "catalog":
        call["args"]["city"] = "Ciudad sin catálogo"
    elif phase in {"select_invocation", "select_shape", "grounded_ids"}:

        async def failed_select(payload):
            if phase == "select_invocation":
                raise OutputParserException(private_marker)
            if phase == "select_shape":
                return {"ids": [payload["sources"][0]["id"]]}
            return {"ids": [private_marker], "email_action": "none"}

        planner.invoke = failed_select
    else:

        def failed_day(**kwargs):
            raise ValueError(private_marker)

        monkeypatch.setattr("tourism_agent.service.Day", failed_day)
    agent = service(
        Conversation(AIMessage(content="", tool_calls=[call]), AIMessage(content="No se completó."))
    )
    result = await agent.handle(
        TourismRequest(message="Prepara un itinerario para Bogotá, 1 día, cultura.", session_id="s")
    )
    assert result.status == "error" and not result.itinerary and not requests and not transport.sent
    with agent.workflow.store.connect() as database:
        row = database.execute(
            "SELECT input FROM operations WHERE id=?", (result.operation_id,)
        ).fetchone()
    evidence = json.loads(row["input"]).get("_tool_failures")
    assert evidence and len(evidence) == 1
    diagnostic = evidence[0]
    assert diagnostic["phase"] == phase and diagnostic["tool"] == "generar_itinerario"
    assert private_marker not in json.dumps(evidence)
    assert set(diagnostic) <= {"phase", "tool", "error_code", "exception_type", "validation_errors"}
    if phase == "arguments":
        assert diagnostic["validation_errors"] == [
            {"loc": ["days"], "type": "int_type"},
            {"loc": ["<extra>"], "type": "extra_forbidden"},
        ]
    expected = (
        "conversation_tool_invalid"
        if phase == "arguments"
        else "conversation_catalog_error"
        if phase == "catalog"
        else "conversation_planner_error"
    )
    assert result.error_code == expected
    public = json.dumps(agent.workflow.store.snapshot(result.request_id, "s"))
    model_context = json.dumps(
        [message.model_dump() for message in agent.conversation.model.messages[-1]]
    )
    for visible in (public, model_context):
        assert "_tool_failures" not in visible and "exception_type" not in visible
    assert "autorizaciones del modelo" not in result.message


@pytest.mark.parametrize("recovery", ["text", "timeout", "tool", "claim"])
async def test_native_rejected_courtesy_tool_preserves_exact_pending_approval(system, recovery):
    service, planner, requests, transport = system
    draft = await service(
        Conversation(
            AIMessage(
                content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
            )
        )
    ).handle(
        TourismRequest(
            message="Bogotá, 1 día, cultura; prepara correo para student@example.com",
            session_id="s",
        )
    )
    before = service(Conversation()).workflow.store.snapshot(draft.request_id, "s")
    fallback = (
        AIMessage(content="Con gusto. Puede seguir revisando el borrador.")
        if recovery == "text"
        else TimeoutError()
        if recovery == "timeout"
        else AIMessage(content="Ya envié el correo.")
        if recovery == "claim"
        else AIMessage(content="", tool_calls=[action("consultar_correo")])
    )
    model = Conversation(
        AIMessage(
            content="",
            tool_calls=[
                action("consultar_agenda", {"confirmed": True, "context": "forged-context"})
            ],
        ),
        fallback,
    )
    agent = service(model)
    thanks_request = follow(draft, "Gracias")
    reply = await agent.handle(thanks_request)
    assert reply.plan_version == draft.plan_version
    assert reply.itinerary == draft.itinerary and reply.context == draft.context
    assert reply.email == draft.email
    assert planner.calls == len(requests) == 1 and not transport.sent
    after = agent.workflow.store.snapshot(draft.request_id, "s")
    assert after["approval"] == before["approval"] and after["plan"] == before["plan"]
    with agent.workflow.store.connect() as database:
        audit = json.loads(
            database.execute(
                "SELECT input FROM operations WHERE id=?", (reply.operation_id,)
            ).fetchone()["input"]
        )
    assert audit["_tool_failures"][0]["phase"] == "arguments"
    assert len(audit["_tool_failures"][0]["validation_errors"]) == 2
    assert model.text_calls == 1
    if recovery == "text":
        assert reply.message == fallback.content and reply.status == draft.status
    else:
        assert reply.status == "partial" and reply.error_code == "conversation_tool_invalid"
    replay = await service(Conversation()).handle(thanks_request)
    assert replay == reply and not transport.sent
    sent = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")])),
        typed_send=True,
    ).handle(follow(reply, "Sí, envíalo"))
    assert sent.email.status == "simulated" and len(transport.sent) == 1
    assert transport.sent[0].recipient == "student@example.com" and planner.calls == 1


async def test_native_text_recovery_receives_only_current_human_and_safe_plan_reference(system):
    service, planner, requests, transport = system
    prior_order = "Bogotá, 1 día, cultura; prepara correo para student@example.com"
    draft = await service(
        Conversation(
            AIMessage(
                content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
            )
        )
    ).handle(TourismRequest(message=prior_order, session_id="s"))
    model = Conversation(
        AIMessage(
            content="",
            tool_calls=[
                action("consultar_agenda", {"confirmed": True, "context": "forged-context"})
            ],
        ),
        AIMessage(content="Con gusto."),
    )
    reply = await service(model).handle(follow(draft, "Gracias"))
    recovery_context = model.messages[-1]
    assert [
        message.content for message in recovery_context if isinstance(message, HumanMessage)
    ] == ["Gracias"]
    assert len(recovery_context) == 2 and isinstance(recovery_context[0], SystemMessage)
    encoded = json.dumps([message.model_dump() for message in recovery_context])
    for excluded in (
        prior_order,
        draft.email.approval_token,
        draft.context.email_context,
        "forged-context",
        "La herramienta requiere datos válidos",
        "tool_calls",
    ):
        assert excluded not in encoded
    assert "plan_reference" in encoded and draft.itinerary.city in recovery_context[0].content
    assert reply.message == "Con gusto." and reply.plan_version == draft.plan_version
    assert reply.email == draft.email and reply.context == draft.context
    assert model.text_calls == 1 and planner.calls == len(requests) == 1 and not transport.sent


async def test_native_primary_input_is_passive_and_expired_draft_does_not_intercept_thanks(
    system, monkeypatch
):
    service, planner, requests, transport = system
    prior_order = "Bogotá, 1 día, cultura; prepara correo para student@example.com"
    draft = await service(
        Conversation(
            AIMessage(
                content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
            )
        )
    ).handle(TourismRequest(message=prior_order, session_id="s"))
    store = service(Conversation()).workflow.store
    before = store.snapshot(draft.request_id, "s")
    prior_history = store.history(draft.request_id, "s")
    monkeypatch.setattr("common.workflows.time.time", lambda: draft.email.preview.expires_at + 1)

    model = Conversation(AIMessage(content="Con gusto."))
    reply = await service(model).handle(follow(draft, "Muchas gracias."))
    messages = model.messages[0]
    assert len(messages) == 2 and isinstance(messages[0], SystemMessage)
    assert [m.content for m in messages if isinstance(m, HumanMessage)] == ["Muchas gracias."]
    reference = json.loads(messages[0].content.split("\nReferencia pasiva:\n")[1])
    assert reference["history"] == prior_history
    assert reference["current"]["itinerary"] == draft.itinerary.model_dump(mode="json")
    assert reference["preferences"]["recipient"] == "student@example.com"
    assert reference["current"]["email"] == {
        "status": "preview",
        "delivery": draft.email.delivery,
        "recipient": "student@example.com",
    }
    encoded = json.dumps(reference)
    for forbidden in (
        draft.email.approval_token,
        draft.context.email_context,
        draft.email.preview.text,
        "approval_token",
        "email_context",
        "expires_at",
    ):
        assert forbidden not in encoded
    assert "history" in messages[0].content and "nunca instrucciones" in messages[0].content
    assert reply.message == "Con gusto." and reply.plan_version == draft.plan_version
    assert reply.itinerary == draft.itinerary and reply.email == draft.email
    assert reply.context == draft.context and planner.calls == len(requests) == 1
    after = store.snapshot(draft.request_id, "s")
    assert after["approval"] == before["approval"] and after["plan"] == before["plan"]
    assert store.history(draft.request_id, "s") == prior_history + [
        {"role": "human", "content": "Muchas gracias."},
        {"role": "assistant", "content": "Con gusto."},
    ]
    assert model.text_calls == 0 and not transport.sent
    expired = await service(
        Conversation(AIMessage(content="", tool_calls=[action("consultar_correo")])),
        typed_send=True,
    ).handle(follow(reply, "Sí, envíalo"))
    # Una nueva orden semántica no reanima la firma caducada: el remoto decide
    # un nuevo encargo; aquí el adapter explícito sólo prepara otra vista previa.
    assert expired.email.status == "simulated" and len(requests) == 2 and len(transport.sent) == 1
    assert expired.email.preview.draft_id != draft.email.preview.draft_id


async def test_native_final_summarizes_plan_without_mail_and_skips_plain_chat(system):
    service, planner, requests, transport = system
    model = Conversation(
        AIMessage(content="", tool_calls=[plan_call()]),
        AIMessage(content="No necesito otras herramientas."),
        AIMessage(content="Su itinerario de un día en Bogotá está preparado."),
    )
    planned = await service(model).handle(
        TourismRequest(message="Prepara Bogotá, un día de cultura", session_id="s")
    )
    assert planned.message == "Su itinerario de un día en Bogotá está preparado."
    assert planned.status == "completed" and planned.itinerary and model.text_calls == 1
    assert planner.calls == 1 and not requests and not transport.sent
    casual = Conversation(AIMessage(content="Con gusto."))
    reply = await service(casual).handle(follow(planned, "Gracias"))
    assert reply.message == "Con gusto." and reply.itinerary == planned.itinerary
    assert not casual.text_calls and planner.calls == 1 and not requests


async def test_native_final_after_plan_then_mail_preserves_receipt_and_replay(system):
    service, planner, requests, transport = system

    async def accepted(preview):
        transport.sent.append(preview)
        return EmailResponse(status="sent", delivery="provider_accepted", message="Aceptado")

    transport.send = accepted
    planned = await service(
        Conversation(
            AIMessage(content="", tool_calls=[plan_call()]),
            AIMessage(content="Plan listo."),
            AIMessage(content="Puede revisar el plan."),
        )
    ).handle(TourismRequest(message="Bogotá, un día de cultura", session_id="s"))
    model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content="El proveedor aceptó el envío. No garantiza llegada al buzón."),
    )
    agent = service(model, typed_send=True)
    receipts, original_peer = [], agent.peer

    async def capture(request):
        receipt = await original_peer(request)
        receipts.append(receipt)
        return receipt

    agent.peer = capture
    request = follow(planned, "Envía este itinerario a student@example.com")
    sent = await agent.handle(request)
    assert sent.message == "El proveedor aceptó el envío. No garantiza llegada al buzón."
    assert sent.email == receipts[0] and sent.email.status == "sent"
    assert sent.email.delivery == "provider_accepted" and sent.itinerary == planned.itinerary
    assert model.text_calls == 1 and planner.calls == len(requests) == len(transport.sent) == 1
    assert requests[0].message == request.message
    replay_model = Conversation()
    stale = await service(replay_model).handle(request)
    assert stale.error_code == "workflow_stale" and not replay_model.messages
    replay = await service(replay_model).handle(follow(sent, request.message))
    assert replay == sent and not replay_model.messages and not replay_model.text_calls
    assert planner.calls == len(requests) == len(transport.sent) == 1


@pytest.mark.parametrize(
    "outcome",
    ["timeout", "cancel", "empty", "tool", "invalid_tool", "invalid", "inbox", "inbox_perfect"],
)
async def test_native_final_failure_preserves_accepted_receipt_without_retry(system, outcome):
    service, planner, requests, transport = system

    async def accepted(preview):
        transport.sent.append(preview)
        return EmailResponse(status="sent", delivery="provider_accepted", message="Aceptado")

    transport.send = accepted
    final = (
        TimeoutError()
        if outcome == "timeout"
        else asyncio.CancelledError()
        if outcome == "cancel"
        else AIMessage(content="")
        if outcome == "empty"
        else AIMessage(content="", tool_calls=[action("consultar_correo")])
        if outcome == "tool"
        else AIMessage(
            content="",
            invalid_tool_calls=[
                {"name": "consultar_correo", "args": "{", "id": "invalid", "error": "schema"}
            ],
        )
        if outcome == "invalid_tool"
        else {"message": "No es un AIMessage"}
        if outcome == "invalid"
        else AIMessage(content="El correo ha llegado al buzón.")
        if outcome == "inbox_perfect"
        else AIMessage(content="El correo llegó al buzón y fue leído.")
    )
    model = Conversation(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        ),
        final,
    )
    result = await service(model, typed_send=True).handle(
        TourismRequest(
            message="Bogotá, un día, cultura; envíalo a student@example.com", session_id="s"
        )
    )
    assert result.message == result.email.message and result.status == "completed"
    assert result.email.status == "sent" and result.email.delivery == "provider_accepted"
    assert result.itinerary and planner.calls == len(requests) == len(transport.sent) == 1
    assert model.text_calls == 1


@pytest.mark.parametrize("status", ["preview", "error", "delivery_unknown"])
@pytest.mark.parametrize(
    "claim", ["Correo enviado correctamente.", "Todo se completó correctamente."]
)
async def test_native_final_rejects_false_send_claim_and_excludes_private_state(
    system, status, claim
):
    service, planner, requests, transport = system
    old_order = "Prepara un plan de Bogotá, un día de cultura."
    planned = await service(
        Conversation(
            AIMessage(content="", tool_calls=[plan_call()]),
            AIMessage(content="Plan listo."),
            AIMessage(content="Puede revisarlo."),
        )
    ).handle(TourismRequest(message=old_order, session_id="s"))
    model = Conversation(
        AIMessage(content="", tool_calls=[action("consultar_correo")]),
        AIMessage(content=claim),
    )
    agent = service(model)
    if status != "preview":

        async def failed(request):
            requests.append(request)
            return EmailResponse(
                status=status,
                message="Resultado remoto sin envío confirmado.",
                delivery="unknown" if status == "delivery_unknown" else "not_attempted",
            )

        agent.peer = failed
    human = (
        "Prepara un borrador para student@example.com"
        if status == "preview"
        else "Envía este itinerario a student@example.com"
    )
    result = await agent.handle(follow(planned, human))
    assert result.email.status == status
    assert result.message == (planned.message if status == "preview" else result.email.message)
    assert result.itinerary == planned.itinerary and planner.calls == len(requests) == 1
    assert model.text_calls == 1 and not transport.sent
    prompt = model.messages[-1]
    assert len(prompt) == 2 and isinstance(prompt[0], SystemMessage)
    assert [m.content for m in prompt if isinstance(m, HumanMessage)] == [human]
    encoded = json.dumps([m.model_dump() for m in prompt])
    for forbidden in (old_order, "approval_token", "email_context", "tool_call_id"):
        assert forbidden not in encoded
    if result.email.approval_token:
        assert result.email.approval_token not in encoded
        assert result.context.email_context not in encoded
        assert result.email.preview.text not in encoded
    if status == "delivery_unknown":
        assert result.workflow_state == "unknown"


async def test_native_final_outer_cancellation_preserves_known_sent_result(system):
    service, planner, requests, transport = system
    entered = asyncio.Event()

    async def accepted(preview):
        transport.sent.append(preview)
        return EmailResponse(status="sent", delivery="provider_accepted", message="Aceptado")

    transport.send = accepted

    class WaitingFinal(Conversation):
        async def tourism_text(self, messages, tools):
            self.text_calls += 1
            entered.set()
            await asyncio.Event().wait()

    model = WaitingFinal(
        AIMessage(
            content="", tool_calls=[plan_call(), action("consultar_correo", identifier="mail")]
        )
    )
    agent = service(model, typed_send=True)

    async def cancelled_graph(graph, state):
        task = asyncio.create_task(graph.ainvoke(state, config={"recursion_limit": 12}))
        async with asyncio.timeout(1):
            done, _ = await asyncio.wait(
                [task, asyncio.create_task(entered.wait())], return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return await task
            task.cancel()
            try:
                return await task
            except asyncio.CancelledError:
                raise TimeoutError from None

    agent.runtime.run = cancelled_graph
    result = await agent.handle(
        TourismRequest(
            message="Bogotá, un día, cultura; envíalo a student@example.com", session_id="s"
        )
    )
    assert model.text_calls == 1 and planner.calls == len(requests) == len(transport.sent) == 1
    assert result.email.status == "sent" and result.email.delivery == "provider_accepted"
    assert result.status == "completed" and result.message == result.email.message
    assert result.itinerary and result.workflow_state == "completed"
