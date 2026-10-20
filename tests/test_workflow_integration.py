import pytest
from semantic_fixtures import conversation
from test_email_chat_graph import SemanticModel

from common.config import Settings
from common.contracts import EmailChatRequest, EmailRequest, EmailResponse, TourismRequest
from email_agent.service import EmailService


class Transport:
    config_digest = "synthetic-transport"

    def __init__(self):
        self.sent = []

    async def send(self, preview):
        self.sent.append(preview)
        return EmailResponse(
            status="simulated", message="Prueba hermética", delivery="simulated_only"
        )


async def test_email_restart_recovers_draft_without_client_context_and_replays_once(tmp_path):
    settings = Settings.offline(
        approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")
    )
    transport = Transport()
    first = await EmailService(settings, transport=transport, model=SemanticModel("draft")).chat(
        EmailChatRequest(
            message="Redacta un borrador para student@example.com que diga hola", session_id="s"
        )
    )
    assert first.request_id and first.plan_version == 1
    restarted = EmailService(
        settings, transport=transport, model=SemanticModel("send", reuse_draft=True)
    )
    request = EmailChatRequest(
        message="Sí, envíalo",
        request_id=first.request_id,
        plan_version=first.plan_version,
        session_id="s",
    )
    sent = await restarted.chat(request)
    replay = await EmailService(settings, transport=transport, model=SemanticModel("send")).chat(
        request.model_copy(update={"plan_version": sent.plan_version})
    )
    assert sent.status == replay.status == "simulated" and len(transport.sent) == 1


async def test_itinerary_email_owner_and_receipt_survive_replay(tmp_path):
    from tourism_agent.service import TourismService

    transport = Transport()
    email = EmailService(
        Settings.offline(approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")),
        transport=transport,
        model=SemanticModel("send"),
    )
    settings = Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite"))
    request = TourismRequest(
        message="Bogotá 2 días cultura; envíalo a student@example.com",
        session_id="s",
        request_id="joint",
    )
    first = await TourismService(
        settings,
        peer=email.handle,
        conversation_model=conversation("generar_itinerario", "consultar_correo"),
    ).handle(request)
    again = await TourismService(
        settings,
        peer=email.handle,
        conversation_model=conversation("generar_itinerario", "consultar_correo"),
    ).handle(request)
    assert first.email.status == again.email.status == "simulated" and len(transport.sent) == 1
    snapshot = TourismService(settings).workflow.store.snapshot("joint", "s")
    assert snapshot["operations"][0]["receipt"]["email"]["status"] == "simulated"


async def test_equal_initial_trip_data_does_not_resume_another_conversation(tmp_path):
    from tourism_agent.service import TourismService

    settings = Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite"))
    request = TourismRequest(context={"city": "Bogota", "days": 2, "interests": ["culture"]})
    first = await TourismService(settings).handle(request)
    second = await TourismService(settings).handle(request)
    assert first.status == second.status == "completed"
    assert first.request_id != second.request_id
    assert first.context.request_id == first.request_id


async def test_calendar_not_authorized_is_durable_blocked_not_complete(tmp_path):
    from test_agenda import Model

    from agenda_agent.contracts import AgendaRequest, AgendaSpec
    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService

    settings = Settings.offline(
        agenda_approval_secret="a" * 40, workflow_db_path=str(tmp_path / "agenda.sqlite")
    )
    service = AgendaService(
        settings, model=Model(), ledger=AgendaLedger(tmp_path / "effects.sqlite")
    )
    request = AgendaRequest(
        message="Organiza repaso",
        session_id="s",
        spec=AgendaSpec.model_validate(Model().result["spec"]),
    )
    response = await service.handle(request)
    assert response.error_code == "calendar_not_configured" and response.workflow_state == "blocked"
    state = service.workflow.store.snapshot(response.request_id, "s")
    assert state["state"] == "blocked" and not state["response"]["events"]


async def test_calendar_block_can_be_reassessed_after_authorization_with_new_message(tmp_path):
    from test_agenda import Calendar, Model

    from agenda_agent.contracts import AgendaRequest, AgendaSpec
    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService

    settings = Settings.offline(
        agenda_approval_secret="a" * 40, workflow_db_path=str(tmp_path / "agenda.sqlite")
    )
    ledger = AgendaLedger(tmp_path / "effects.sqlite")
    first = await AgendaService(settings, model=Model(), ledger=ledger).handle(
        AgendaRequest(
            message="Organiza repaso",
            session_id="s",
            spec=AgendaSpec.model_validate(Model().result["spec"]),
        )
    )
    calendar = Calendar()
    response = await AgendaService(
        settings, model=Model(), calendar=calendar, ledger=ledger
    ).handle(
        AgendaRequest(
            message="Ya autoricé Calendar; continúa con el repaso",
            session_id="s",
            request_id=first.request_id,
            plan_version=first.plan_version,
        )
    )
    assert response.status == "preview" and response.workflow_state == "awaiting_approval"
    assert response.request_id == first.request_id and not calendar.created


async def test_itinerary_resumes_blocked_agenda_without_repeating_known_dates(tmp_path):
    from test_agenda import Calendar, Model

    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService
    from tourism_agent.service import TourismService

    agenda_settings = Settings.offline(
        agenda_approval_secret="a" * 40, workflow_db_path=str(tmp_path / "agenda.sqlite")
    )
    ledger = AgendaLedger(tmp_path / "effects.sqlite")
    blocked = AgendaService(agenda_settings, model=Model(), ledger=ledger)
    settings = Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite"))
    first = await TourismService(
        settings,
        agenda_peer=blocked.handle,
        conversation_model=conversation("generar_itinerario", "consultar_agenda"),
    ).handle(
        TourismRequest(
            message=(
                "Bogotá 2 días cultura; agenda desde 2030-05-06 "
                "a las 10:00 America/Bogota 60 minutos"
            ),
            session_id="s",
        )
    )
    assert first.workflow_state == "blocked"
    calendar = Calendar()
    ready = AgendaService(agenda_settings, model=Model(), calendar=calendar, ledger=ledger)
    resumed = await TourismService(
        settings, agenda_peer=ready.handle, conversation_model=conversation("consultar_agenda")
    ).handle(
        TourismRequest(
            message="Ya autoricé Calendar; continúa",
            request_id=first.request_id,
            session_id="s",
            plan_version=first.plan_version,
        )
    )
    assert resumed.agenda.status == "preview" and not calendar.created
    assert resumed.plan_version == resumed.agenda.plan_version == 2


async def test_recipient_revision_invalidates_old_approval_and_keeps_new_version(tmp_path):
    settings = Settings.offline(
        approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")
    )
    transport = Transport()
    service = EmailService(settings, transport=transport, model=SemanticModel("send"))
    original = await service.handle(
        EmailRequest(recipient="first@example.com", request="Hola", session_id="s")
    )
    changed = await service.handle(
        EmailRequest(
            recipient="second@example.com",
            request="Hola",
            request_id=original.request_id,
            plan_version=1,
            session_id="s",
        )
    )
    assert changed.plan_version == changed.preview.plan_version == 2
    assert changed.preview.recipient == "second@example.com"
    stale = await service.chat(
        EmailChatRequest(
            message="Sí, envíalo",
            request_id=original.request_id,
            plan_version=1,
            context=original.context,
            session_id="s",
        )
    )
    assert stale.error_code == "workflow_stale" and not transport.sent
    legacy = await service.chat(
        EmailChatRequest(message="Sí, envíalo", context=original.context, session_id="s")
    )
    assert legacy.error_code == "workflow_stale" and not transport.sent


@pytest.mark.parametrize("envelope", ["old", "mixed", "new_operation"])
async def test_old_email_approval_cannot_be_relabelled_with_current_wrapper_version(
    tmp_path, envelope
):
    settings = Settings.offline(
        approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")
    )
    transport = Transport()
    service = EmailService(settings, transport=transport, model=SemanticModel("send"))
    original = await service.handle(
        EmailRequest(recipient="first@example.com", request="Hola", session_id="s")
    )
    changed = await service.handle(
        EmailRequest(
            recipient="second@example.com",
            request="Hola",
            request_id=original.request_id,
            plan_version=1,
            session_id="s",
        )
    )
    result = await service.handle(
        EmailRequest(
            action="send",
            confirmed=True,
            request_id=original.request_id,
            plan_version=changed.plan_version,
            session_id="s",
            preview=changed.preview if envelope == "mixed" else original.preview,
            approval_token=original.approval_token,
            operation_id="forged-new-operation" if envelope == "new_operation" else None,
        )
    )
    assert result.error_code == "workflow_approval_mismatch" and not transport.sent


@pytest.mark.parametrize("integrated", [False, True])
@pytest.mark.parametrize("revision", ["recipient", "text"])
async def test_summary_recipient_revision_preserves_created_events_and_requires_new_mail_approval(
    tmp_path, integrated, revision
):
    from test_agenda import Calendar, Model

    from agenda_agent.contracts import AgendaRequest
    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService
    from tourism_agent.service import TourismService

    transport = Transport()
    email = EmailService(
        Settings.offline(approval_secret="e" * 40, workflow_db_path=str(tmp_path / "email.sqlite")),
        transport=transport,
        model=SemanticModel("send"),
    )
    email.clock = lambda: 100
    calendar = Calendar()
    settings = Settings.offline(
        agenda_approval_secret="a" * 40, workflow_db_path=str(tmp_path / "agenda.sqlite")
    )
    ledger = AgendaLedger(tmp_path / "effects.sqlite")
    agenda = AgendaService(
        settings,
        model=Model(mail_action="send"),
        calendar=calendar,
        ledger=ledger,
        email_peer=email.handle,
        clock=lambda: 100,
    )
    tourism_settings = Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite"))

    async def forbidden(req):
        raise AssertionError("Turismo no debe asumir el resumen de Agenda")

    async def invoke(message, previous=None):
        args = {"message": message, "session_id": "s"}
        if previous:
            args.update(request_id=previous.request_id, plan_version=previous.plan_version)
        if integrated:
            tourism = TourismService(
                tourism_settings,
                peer=forbidden,
                agenda_peer=agenda.handle,
                conversation_model=conversation(
                    *(
                        ["generar_itinerario", "consultar_agenda"]
                        if previous is None
                        else ["consultar_agenda"]
                    ),
                    days=1,
                ),
            )
            tourism.clock = lambda: 100
            return await tourism.handle(TourismRequest(**args))
        return await agenda.handle(AgendaRequest(**args))

    first = await invoke(
        ("Bogotá 1 día cultura; agenda " if integrated else "Organiza repaso ")
        + "2030-05-06 10:00 America/Bogota 60 minutos y envía resumen a first@example.com"
    )
    created = await invoke("Sí, agéndalo", first)
    accepted = created.agenda if integrated else created
    assert accepted.status == "created" and len(calendar.created) == len(transport.sent) == 1
    event_ids = [event.id for event in accepted.events]
    # Reabrir el núcleo debe recuperar los eventos y la solicitud, sin contexto del cliente.
    agenda = AgendaService(
        settings,
        model=Model(mail_action="send"),
        calendar=calendar,
        ledger=ledger,
        email_peer=email.handle,
        clock=lambda: 100,
    )
    email.model = SemanticModel("draft", content_source="message")
    changed = await invoke(
        "Cambia únicamente el destinatario del resumen a second@example.com"
        if revision == "recipient"
        else "Cambia únicamente el texto del resumen a Nuevo resumen revisado",
        created,
    )
    current = changed.agenda if integrated else changed
    assert current.status == "created" and [event.id for event in current.events] == event_ids
    expected_recipient = "second@example.com" if revision == "recipient" else "first@example.com"
    assert current.email["status"] == "completed" and current.email["draft"]
    if revision == "text":
        assert "Nuevo resumen revisado" in current.email["draft"]["text"]
    assert len(calendar.created) == len(transport.sent) == 1
    snapshot = agenda.workflow.store.snapshot(changed.request_id, "s")
    assert snapshot["plan"] and snapshot["response"]["events"][0]["id"] == event_ids[0]
    old_args = dict(
        message="Sí, envíalo",
        request_id=changed.request_id,
        plan_version=changed.plan_version,
        session_id="s",
        context=created.context,
    )
    old = (
        await TourismService(
            tourism_settings,
            peer=forbidden,
            agenda_peer=agenda.handle,
            conversation_model=conversation("consultar_agenda"),
        ).handle(TourismRequest(**old_args))
        if integrated
        else await agenda.handle(AgendaRequest(**old_args))
    )
    assert old.error_code == "workflow_stale" and len(transport.sent) == 1
    email.model = SemanticModel("send", reuse_draft=True)
    confirmed = await invoke("Sí, envíalo", changed)
    receipt = confirmed.agenda if integrated else confirmed
    assert receipt.email["status"] == "simulated" and len(transport.sent) == 2
    assert len(calendar.created) == 1 and str(transport.sent[-1].recipient) == expected_recipient
    replay = await invoke("Sí, envíalo", changed)
    assert replay.request_id == changed.request_id and len(transport.sent) == 2


async def test_old_agenda_context_cannot_be_forked_to_another_request_or_current_version(tmp_path):
    from test_agenda import Calendar, Model

    from agenda_agent.contracts import AgendaRequest, AgendaSpec
    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService

    calendar = Calendar()
    service = AgendaService(
        Settings.offline(
            agenda_approval_secret="a" * 40, workflow_db_path=str(tmp_path / "agenda.sqlite")
        ),
        model=Model(mail_action="send"),
        calendar=calendar,
        ledger=AgendaLedger(tmp_path / "effects.sqlite"),
        clock=lambda: 100,
    )
    spec = AgendaSpec.model_validate(Model().result["spec"])
    original = await service.handle(
        AgendaRequest(message="Organiza repaso", spec=spec, session_id="s")
    )
    revised = await service.handle(
        AgendaRequest(
            message="Cambia la fecha a 2030-05-07",
            spec=spec.model_copy(update={"date": "2030-05-07"}),
            request_id=original.request_id,
            plan_version=1,
            session_id="s",
        )
    )
    assert revised.status == "preview"
    stale = await service.handle(
        AgendaRequest(
            message="Sí, agéndalo",
            context=original.context,
            request_id=original.request_id,
            plan_version=revised.plan_version,
            session_id="s",
        )
    )
    assert stale.error_code == "workflow_stale" and not calendar.created
    fork = await service.handle(
        AgendaRequest(
            message="Continúa",
            context=original.context,
            request_id="forged-request",
            plan_version=1,
            session_id="s",
        )
    )
    assert fork.error_code == "workflow_stale" and not calendar.created


async def test_context_only_compatibility_resolves_original_request_and_session(tmp_path):
    settings = Settings.offline(
        approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")
    )
    transport = Transport()
    draft = await EmailService(settings, transport=transport, model=SemanticModel("send")).handle(
        EmailRequest(recipient="student@example.com", request="Hola")
    )
    reply = await EmailService(settings, transport=transport, model=SemanticModel("send")).chat(
        EmailChatRequest(message="Sí, envíalo", context=draft.context)
    )
    assert reply.request_id == draft.request_id and reply.session_id == draft.session_id
    assert reply.status == "simulated" and len(transport.sent) == 1


async def test_itinerary_agenda_email_has_one_mail_owner_and_restart_receipts(tmp_path):
    from test_agenda import Calendar, Model

    from agenda_agent.ledger import AgendaLedger
    from agenda_agent.service import AgendaService
    from tourism_agent.service import TourismService

    transport = Transport()
    email = EmailService(
        Settings.offline(approval_secret="e" * 40, workflow_db_path=str(tmp_path / "email.sqlite")),
        transport=transport,
        model=SemanticModel("send"),
    )
    calendar = Calendar()
    agenda_settings = Settings.offline(
        agenda_approval_secret="a" * 40, workflow_db_path=str(tmp_path / "agenda.sqlite")
    )
    agenda = AgendaService(
        agenda_settings,
        model=Model(mail_action="send"),
        calendar=calendar,
        ledger=AgendaLedger(tmp_path / "effects.sqlite"),
        email_peer=email.handle,
        clock=lambda: 1_900_000_000,
    )
    tourism_settings = Settings.offline(workflow_db_path=str(tmp_path / "tourism.sqlite"))

    async def forbidden(req):
        raise AssertionError("Turismo no es dueño del correo de Agenda")

    first = await TourismService(
        tourism_settings,
        peer=forbidden,
        agenda_peer=agenda.handle,
        conversation_model=conversation("generar_itinerario", "consultar_agenda", days=3),
    ).handle(
        TourismRequest(
            message=(
                "Bogotá 3 días cultura; agenda 2030-05-06 10:00 America/Bogota 60 "
                "minutos y envía resumen a student@example.com"
            ),
            session_id="s",
        )
    )
    assert first.agenda.status == "preview" and len(calendar.created) == 0
    request = TourismRequest(
        message="Sí, agéndalo",
        request_id=first.request_id,
        plan_version=first.plan_version,
        session_id="s",
    )
    result = await TourismService(
        tourism_settings,
        peer=forbidden,
        agenda_peer=agenda.handle,
        conversation_model=conversation("consultar_agenda"),
    ).handle(request)
    assert result.agenda.status == "created" and len(transport.sent) == 1
    replay = await TourismService(
        tourism_settings,
        peer=forbidden,
        agenda_peer=agenda.handle,
        conversation_model=conversation("consultar_agenda"),
    ).handle(request)
    assert replay.agenda.status == "created" and len(transport.sent) == 1
    agenda_state = agenda.workflow.store.snapshot(first.request_id, "s")
    assert agenda_state["response"]["email"]["request_id"] == first.request_id
    assert agenda_state["response"]["email"]["operation_id"]


async def test_expired_approval_after_restart_cannot_be_reactivated(tmp_path):
    settings = Settings.offline(
        approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")
    )
    transport = Transport()
    service = EmailService(settings, transport=transport, model=SemanticModel("send"))
    service.clock = lambda: 100
    draft = await service.handle(
        EmailRequest(recipient="student@example.com", request="Hola", session_id="s")
    )
    reopened = EmailService(settings, transport=transport, model=SemanticModel("send"))
    reopened.clock = lambda: 401
    result = await reopened.chat(
        EmailChatRequest(message="Sí, envíalo", request_id=draft.request_id, session_id="s")
    )
    assert result.error_code in {"workflow_expired", "approval_invalid"} and not transport.sent


async def test_external_effect_before_receipt_remains_unknown_after_restart(tmp_path):
    settings = Settings.offline(
        approval_secret="k" * 40, workflow_db_path=str(tmp_path / "email.sqlite")
    )
    transport = Transport()
    service = EmailService(settings, transport=transport, model=SemanticModel("send"))

    def crash(*args, **kwargs):
        raise RuntimeError("TEST_CRASH_AFTER_EFFECT")

    service.workflow.store.finish = crash
    request = EmailChatRequest(
        message="Redacta un recordatorio de reunión y envíalo a student@example.com",
        session_id="s",
        request_id="crash",
    )
    with pytest.raises(RuntimeError, match="TEST_CRASH"):
        await service.chat(request)
    assert len(transport.sent) == 1
    unknown = await EmailService(settings, transport=transport, model=SemanticModel("send")).chat(
        request
    )
    assert unknown.status == "delivery_unknown" and len(transport.sent) == 1


async def test_distinct_explicit_mail_is_another_operation_not_the_same_intent(tmp_path):
    from types import SimpleNamespace

    from email_agent.dispatch_ledger import SqliteLedger

    transport = Transport()
    transport.gmail = SimpleNamespace(ledger=SqliteLedger(tmp_path / "gmail-effects.sqlite"))
    original_send = transport.send

    async def send(preview, **kwargs):
        return await original_send(preview)

    transport.send = send
    settings = Settings.offline(
        email_transport="gmail",
        email_sender="sender@example.com",
        real_email_enabled=True,
        gmail_credentials_file="UNUSED_SYNTHETIC",
        approval_secret="k" * 40,
        workflow_db_path=str(tmp_path / "email.sqlite"),
    )
    service = EmailService(settings, transport=transport, model=SemanticModel("send"))
    first = await service.chat(
        EmailChatRequest(
            message="Redacta recordatorio de reunión y envíalo a first@example.com",
            session_id="s",
            request_id="r",
        )
    )
    second = await service.chat(
        EmailChatRequest(
            message="Redacta otro recordatorio de examen y envíalo a second@example.com",
            session_id="s",
            request_id="r",
            plan_version=first.plan_version,
        )
    )
    assert first.status == second.status == "simulated" and len(transport.sent) == 2
    assert second.plan_version == 2
