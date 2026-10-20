import httpx
import pytest
from semantic_fixtures import conversation

from common.a2a_transport import send_typed
from common.config import Settings
from common.contracts import TourismRequest
from tourism_agent.conversation import NativeConversation
from tourism_agent.service import TourismService


async def test_itinerary_calls_agenda_and_keeps_original_continuation(tmp_path):
    from agenda_agent.contracts import AgendaResponse

    calls = []
    email_calls = []

    async def agenda(req):
        calls.append(req)
        return AgendaResponse(
            status="clarification",
            message="¿Qué fecha?",
            context="ORIGINAL_AGENDA_CONTEXT",
            missing_field="date",
        )

    async def email(req):
        email_calls.append(req)

    service = TourismService(
        Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite")),
        peer=email,
        agenda_peer=agenda,
        conversation_model=conversation("generar_itinerario", "consultar_agenda", days=3),
    )
    first = await service.handle(
        TourismRequest(
            message="Bogotá 3 días cultura; agéndalo y envía resumen a student@example.com",
            session_id="joint",
        )
    )
    assert first.itinerary and first.agenda.status == "clarification"
    assert first.context.agenda_context == "ORIGINAL_AGENDA_CONTEXT"
    assert not email_calls
    assert len(calls[0].spec.sessions) == 3
    assert calls[0].spec.sessions[0].duration_minutes is None
    service.conversation = NativeConversation(service, conversation("consultar_agenda"))
    await service.handle(
        TourismRequest(message="2030-05-06", context=first.context, session_id="joint")
    )
    assert calls[1].context == "ORIGINAL_AGENDA_CONTEXT"
    assert service.catalog.calls == 1


async def test_agenda_unavailable_does_not_send_false_confirmation(tmp_path):
    async def failed(req):
        raise TimeoutError

    async def email(req):
        pytest.fail("No corresponde enviar correo antes de crear Agenda")

    service = TourismService(
        Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite")),
        peer=email,
        agenda_peer=failed,
        conversation_model=conversation("generar_itinerario", "consultar_agenda", days=3),
    )
    result = await service.handle(
        TourismRequest(
            message="Bogotá 3 días cultura; agéndalo y envía resumen a student@example.com"
        )
    )
    assert result.status == "partial" and result.itinerary
    assert result.error_code == "agenda_peer_unavailable"


async def test_standalone_fastapi_and_native_a2a_do_not_require_gmail(tmp_path):
    from agenda_agent.app import create_app
    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService

    class Model:
        async def invoke(self, payload):
            return {"intent": "chat", "message": "Puedo organizar actividades.", "spec": {}}

    settings = Settings.offline(public_url="http://127.0.0.1:8003")
    service = AgendaService(
        settings, model=Model(), ledger=AgendaLedger(tmp_path / "agenda.sqlite")
    )
    app = create_app(settings, service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8003"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health")).json()["calendar"] == "not_authorized"
        response = await client.post("/api/chat", json={"message": "Hola"})
        assert response.json()["status"] == "completed"
        result, task = await send_typed(
            "http://127.0.0.1:8003", {"message": "Hola"}, http_client=client
        )
        assert result["status"] == "completed" and task.id
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert any(skill["id"] == "agenda" for skill in card["skills"])
        assert not card["capabilities"].get("streaming", False)


async def test_fastapi_itinerary_agenda_email_use_native_a2a_and_exact_preview(tmp_path):
    from test_agenda import Calendar, Model

    from agenda_agent.app import create_app as agenda_app
    from agenda_agent.contracts import AgendaResponse
    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService
    from common.a2a_transport import mount_a2a
    from common.contracts import EmailRequest, EmailResponse
    from common.runtime import base_app
    from email_agent.service import EmailService
    from tourism_agent.app import create_app as tourism_app

    sends = []
    tasks = []

    class Transport:
        config_digest = "test-only"

        async def send(self, preview):
            sends.append(preview)
            return EmailResponse(
                status="simulated", message="Prueba hermética", delivery="simulated_only"
            )

    email = EmailService(Settings.offline(approval_secret="e" * 40), transport=Transport())
    email.clock = lambda: 1_900_000_000
    eapp = base_app("Correo de prueba", "Sin proveedores")
    mount_a2a(
        eapp,
        email,
        EmailRequest,
        Settings.offline(public_url="http://127.0.0.1:8002"),
        "Correo",
        "Prueba",
        [("draft", "Borrador", "Prueba")],
    )
    apps = {8002: eapp}

    async def route(request):
        return await httpx.ASGITransport(app=apps[request.url.port]).handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:

        async def email_peer(req):
            data, task = await send_typed(
                "http://127.0.0.1:8002", req.model_dump(mode="json"), http_client=client
            )
            tasks.append(("email", task.id))
            return EmailResponse.model_validate(data)

        agenda = AgendaService(
            Settings.offline(agenda_approval_secret="a" * 40),
            model=Model(mail_action="send"),
            calendar=Calendar(),
            ledger=AgendaLedger(tmp_path / "agenda.sqlite"),
            email_peer=email_peer,
            clock=lambda: 1_900_000_000,
        )
        apps[8003] = agenda_app(
            Settings.offline(public_url="http://127.0.0.1:8003"), service=agenda
        )

        async def agenda_peer(req):
            data, task = await send_typed(
                "http://127.0.0.1:8003", req.model_dump(mode="json"), http_client=client
            )
            tasks.append(("agenda", task.id))
            return AgendaResponse.model_validate(data)

        tourism = TourismService(
            Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite")),
            agenda_peer=agenda_peer,
            conversation_model=conversation("generar_itinerario", "consultar_agenda", days=3),
        )
        apps[8001] = tourism_app(Settings.offline(), service=tourism)
        first = (
            await client.post(
                "http://127.0.0.1:8001/api/chat",
                json={
                    "message": (
                        "Bogota 3 días cultura; agenda el 2030-05-06 a las 10:00 en "
                        "America/Bogota, 60 minutos por día y envía resumen a "
                        "student@example.com"
                    ),
                    "session_id": "joint",
                },
            )
        ).json()
        assert first["agenda"]["status"] == "preview" and not sends
        assert len(first["agenda"]["preview"]["events"]) == 3
        tourism.conversation = NativeConversation(tourism, conversation("consultar_agenda"))
        request = {"message": "Sí, agéndalo", "session_id": "joint", "context": first["context"]}
        result = (await client.post("http://127.0.0.1:8001/api/chat", json=request)).json()
        assert result["agenda"]["status"] == "created"
        assert result["email"]["status"] == "simulated"
        assert len(sends) == 1 and sends[0].recipient == "student@example.com"
        for event in result["agenda"]["events"]:
            assert event["link"] in sends[0].text and event["title"] in sends[0].text
        await client.post("http://127.0.0.1:8001/api/chat", json=request)
        assert len(sends) == 1 and len(agenda.calendar.created) == 3
        assert [name for name, _ in tasks].count("email") == 2
