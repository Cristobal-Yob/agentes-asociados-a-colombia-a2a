import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from common.config import Settings
from common.model import ModelPort


def test_model_fixture_agenda_has_distinct_structured_operation():
    prompt = SimpleNamespace(
        messages=[
            SimpleNamespace(
                content=json.dumps({"operation": "agenda", "request": "Hola", "previous": {}})
            )
        ]
    )
    result = json.loads(ModelPort._fixture(prompt))
    assert result["intent"] == "chat"
    assert result["message"]


@pytest.mark.parametrize(
    "message,expected_status,missing",
    [
        ("Hola, ¿qué puedes hacer?", "completed", None),
        ("Organiza sesiones de repaso", "clarification", "date"),
    ],
)
async def test_real_vertex_chat_envelope_without_intent_is_safe_without_calendar(
    tmp_path, message, expected_status, missing
):
    Request, _, _, _ = agenda_types()
    model = ModelPort(Settings.offline())

    class Chain:
        async def ainvoke(self, payload, config):
            # Forma observada en la llamada real a Vertex: solo message, sin intent.
            return {"message": "Puedo ayudar a organizar sus actividades."}

    model.chain = Chain()
    model.agenda_chain = model.chain
    agent = service(tmp_path, model=model)
    agent.calendar = None
    response = await agent.handle(Request(message=message))
    assert response.status == expected_status and response.missing_field == missing
    assert not response.events and response.preview is None


@pytest.mark.parametrize(
    "extra",
    [{"spec": {"date": "2030-05-06"}}, {"events": []}, {"confirmed": True}, {"message": " "}],
)
async def test_missing_intent_with_actions_or_preferences_is_not_normalized(tmp_path, extra):
    Request, _, _, _ = agenda_types()
    model = ModelPort(Settings.offline())

    class Chain:
        async def ainvoke(self, payload, config):
            return {"message": "Creado", **extra}

    model.agenda_chain = Chain()
    agent = service(tmp_path, model=model)
    result = await agent.handle(Request(message="Hola"))
    assert result.status == "error" and not agent.calendar.created


def agenda_types():
    from agenda_agent.contracts import AgendaRequest, AgendaSpec, SessionSpec
    from agenda_agent.service import AgendaService

    return AgendaRequest, AgendaSpec, SessionSpec, AgendaService


class Model:
    def __init__(self, result=None, *, mail_action="none"):
        self.result = result or {
            "intent": "plan",
            "message": "",
            "spec": {
                "date": "2030-05-06",
                "timezone": "America/Bogota",
                "start_time": "10:00",
                "sessions": [{"title": "Repaso", "duration_minutes": 60}],
            },
        }
        self.calls = 0
        self.mail_action = mail_action

    async def invoke(self, payload):
        self.calls += 1
        assert payload["operation"] == "agenda"
        return {
            **self.result,
            "mail_action": self.mail_action,
            "mail_evidence": payload["request"] if self.mail_action == "send" else "",
        }


class Calendar:
    fingerprint = "test-calendar"
    account = "sender@example.com"

    def __init__(self):
        self.busy = []
        self.created = []
        self.events = {}

    async def availability(self, start, end):
        return self.busy

    async def create(self, event_id, event, plan_id):
        self.created.append(event_id)
        value = {
            "id": event_id,
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat()},
            "end": {"dateTime": event.end.isoformat()},
            "htmlLink": "https://www.google.com/calendar/event?eid=test",
            "extendedProperties": {"private": {"agendaPlan": plan_id}},
        }
        self.events[event_id] = value
        return value

    async def get(self, event_id):
        return self.events.get(event_id)


def service(tmp_path, model=None, calendar=None, peer=None):
    _, _, _, Service = agenda_types()
    from agenda_agent.ledger import AgendaLedger

    return Service(
        Settings.offline(agenda_approval_secret="k" * 40),
        model=model or Model(),
        calendar=calendar or Calendar(),
        ledger=AgendaLedger(tmp_path / "agenda.sqlite"),
        email_peer=peer,
        clock=lambda: 1_900_000_000,
    )


async def test_standalone_preview_then_original_confirmation_creates_once(tmp_path):
    Request, _, _, _ = agenda_types()
    calendar = Calendar()
    model = Model()
    agent = service(tmp_path, model, calendar)
    draft = await agent.handle(
        Request(
            message="Organiza una sesión el 2030-05-06 a las 10:00 en America/Bogota de 60 minutos",
            session_id="s",
        )
    )
    assert draft.status == "preview" and not calendar.created
    assert draft.preview.events[0].start.isoformat() == "2030-05-06T10:00:00-05:00"
    request = Request(message="Sí, agéndalo", context=draft.context, session_id="s")
    sent = await agent.handle(request)
    repeated = await agent.handle(request)
    assert sent.status == repeated.status == "created"
    assert sent.events[0].id == repeated.events[0].id
    assert len(calendar.created) == 1 and model.calls == 1


async def test_chat_and_missing_fields_do_not_touch_calendar(tmp_path):
    Request, _, _, _ = agenda_types()
    agent = service(
        tmp_path, Model({"intent": "chat", "message": "Puedo organizar sesiones.", "spec": {}})
    )
    assert (await agent.handle(Request(message="Hola"))).status == "completed"
    agent.model = Model(
        {
            "intent": "plan",
            "message": "",
            "spec": {"sessions": [{"title": "Estudio", "duration_minutes": 60}]},
        }
    )
    missing = await agent.handle(Request(message="Organiza estudio"))
    assert missing.status == "clarification" and missing.missing_field == "date"
    assert not agent.calendar.created


async def test_invalid_model_cannot_authorize_or_invent_schema(tmp_path):
    Request, _, _, _ = agenda_types()
    agent = service(tmp_path, Model({"intent": "create", "confirmed": True, "spec": {}}))
    result = await agent.handle(Request(message="Hola"))
    assert result.status == "error" and not agent.calendar.created


@pytest.mark.parametrize(
    "zone,date,start",
    [
        ("Wrong/Zone", "2030-05-06", "10:00"),
        ("America/New_York", "2030-03-10", "02:30"),
        ("America/New_York", "2030-11-03", "01:30"),
    ],
)
async def test_invalid_and_ambiguous_local_times_are_not_scheduled(tmp_path, zone, date, start):
    Request, _, _, _ = agenda_types()
    model = Model(
        {
            "intent": "plan",
            "message": "",
            "spec": {
                "date": date,
                "timezone": zone,
                "start_time": start,
                "sessions": [{"title": "Estudio", "duration_minutes": 60}],
            },
        }
    )
    agent = service(tmp_path, model)
    result = await agent.handle(
        Request(message=f"Organiza estudio {date} {start} {zone} 60 minutos")
    )
    assert (
        result.status == "clarification"
        and result.error_code == "local_time_invalid"
        and not agent.calendar.created
    )


async def test_overlap_revalidated_before_write_and_tampering_rejected(tmp_path):
    Request, _, _, _ = agenda_types()
    calendar = Calendar()
    agent = service(tmp_path, calendar=calendar)
    draft = await agent.handle(
        Request(
            message="Organiza una sesión el 2030-05-06 a las 10:00 en America/Bogota de 60 minutos",
            session_id="s",
        )
    )
    invalid = await agent.handle(
        Request(message="Sí, agéndalo", context=draft.context + "x", session_id="s")
    )
    assert invalid.error_code == "approval_invalid"
    calendar.busy = [(datetime(2030, 5, 6, 15, tzinfo=UTC), datetime(2030, 5, 6, 16, tzinfo=UTC))]
    conflict = await agent.handle(
        Request(message="Sí, agéndalo", context=draft.context, session_id="s")
    )
    assert conflict.status == "conflict" and not calendar.created


async def test_timeout_readback_and_no_blind_duplicate(tmp_path):
    Request, _, _, _ = agenda_types()

    class Lost(Calendar):
        async def create(self, *args):
            await super().create(*args)
            raise httpx.ReadTimeout("respuesta perdida")

    calendar = Lost()
    agent = service(tmp_path, calendar=calendar)
    draft = await agent.handle(
        Request(
            message="Organiza una sesión el 2030-05-06 a las 10:00 en America/Bogota de 60 minutos",
            session_id="s",
        )
    )
    request = Request(message="Sí, agéndalo", context=draft.context, session_id="s")
    assert (await agent.handle(request)).status == "created"
    assert (await agent.handle(request)).status == "created"
    assert len(calendar.created) == 1


async def test_failed_calendar_never_sends_false_summary(tmp_path):
    Request, _, _, _ = agenda_types()
    calls = []

    class Failed(Calendar):
        async def create(self, *args):
            raise httpx.ReadTimeout("sin respuesta")

    async def peer(req):
        calls.append(req)

    agent = service(tmp_path, calendar=Failed(), peer=peer)
    draft = await agent.handle(
        Request(
            message=(
                "Prepara agenda el 2030-05-06 a las 10:00 en America/Bogota de 60 "
                "minutos y envía resumen a student@example.com"
            ),
            recipient="student@example.com",
            session_id="s",
        )
    )
    result = await agent.handle(
        Request(message="Sí, agéndalo", context=draft.context, session_id="s")
    )
    assert result.status == "creation_unknown" and not calls


async def test_created_events_delegate_exact_verified_summary_once(tmp_path):
    from common.contracts import EmailPreview, EmailResponse

    Request, _, _, _ = agenda_types()
    calls = []

    async def peer(req):
        calls.append(req)
        if req.action == "draft":
            return EmailResponse(
                status="preview",
                message="Revise",
                approval_token="TEST_APPROVAL",
                preview=EmailPreview(
                    draft_id="d",
                    session_id="s",
                    recipient=req.recipient,
                    subject=req.content.subject,
                    text=req.content.text,
                    sender="sender@example.com",
                    transport="gmail",
                    expires_at=1_900_000_300,
                ),
            )
        assert req.confirmed and req.approval_token == "TEST_APPROVAL"
        assert req.preview.recipient == "student@example.com"
        return EmailResponse(status="sent", message="Aceptado", delivery="provider_accepted")

    agent = service(tmp_path, peer=peer, model=Model(mail_action="send"))
    draft = await agent.handle(
        Request(
            message=(
                "Agenda 2030-05-06 10:00 America/Bogota 60 minutos y envía por "
                "correo a student@example.com"
            ),
            session_id="s",
        )
    )
    req = Request(message="Sí, agéndalo", context=draft.context, session_id="s")
    result = await agent.handle(req)
    assert result.status == "created" and result.email["status"] == "sent"
    assert result.events[0].link in calls[0].content.text
    await agent.handle(req)
    assert len(calls) == 2


async def test_expired_and_other_session_approval_never_create(tmp_path):
    Request, _, _, _ = agenda_types()
    agent = service(tmp_path)
    draft = await agent.handle(
        Request(message="Agenda 2030-05-06 10:00 America/Bogota 60 minutos", session_id="s")
    )
    result = await agent.handle(
        Request(message="Sí, agéndalo", context=draft.context, session_id="other")
    )
    assert result.error_code == "approval_invalid"
    agent.clock = lambda: 1_900_000_301
    result = await agent.handle(
        Request(message="Sí, agéndalo", context=draft.context, session_id="s")
    )
    assert result.error_code == "approval_invalid" and not agent.calendar.created


async def test_model_cannot_invent_missing_human_dates_or_duration(tmp_path):
    Request, _, _, _ = agenda_types()
    agent = service(tmp_path)
    result = await agent.handle(Request(message="Organiza repaso"))
    assert result.status == "clarification" and result.missing_field == "date"
    assert not agent.calendar.created
