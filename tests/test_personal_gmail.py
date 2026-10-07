import base64
import json
from email import policy
from email.parser import BytesParser

import pytest

from common.config import Settings
from email_agent.gmail_auth import SENDER, OAuthError, validate_credentials
from tests.test_email_intent import integrated
from tests.test_message_chat import gmail_chat

pytestmark = pytest.mark.asyncio


async def test_fixed_personal_sender_default_and_old_grant_rejected(monkeypatch):
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    assert SENDER == "student@example.com"
    assert Settings.from_env("email").email_sender == SENDER
    from email_agent.gmail_auth import SCOPES, TOKEN_URL

    with pytest.raises(OAuthError):
        validate_credentials(
            dict(
                type="authorized_user",
                project_id="example-project",
                account_email="learner@example.com",
                account_sub="old",
                client_id="test.apps.googleusercontent.com",
                client_secret="fixture",
                refresh_token="fixture",
                scopes=list(SCOPES),
                token_uri=TOKEN_URL,
            )
        )


async def test_clear_compose_and_send_uses_personal_identity_and_arbitrary_recipient(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        reply = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Redacta un recordatorio de reunión mañana y envíalo "
                    "a invitado@example.com desde intruso@example.com"
                },
            )
        ).json()
        assert reply["status"] == "clarification" and not posts
        reply = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Redacta un recordatorio de reunión mañana y envíalo "
                    "a invitado@example.com"
                },
            )
        ).json()
        assert reply["status"] == "sent" and reply["delivery"] == "provider_accepted"
        assert reply["preview"]["sender"] == "student@example.com"
        mime = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(json.loads(posts[0].content)["raw"])
        )
        assert mime["From"] == "student@example.com" and mime["To"] == "invitado@example.com"
        assert len(posts) == 1


async def test_normal_chat_uses_llm_and_never_requests_email_fields(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        seen = []

        class ChatModel:
            async def invoke(self, payload):
                seen.append(payload)
                return {
                    "message": "Hola. Puedo conversar o redactar y enviar correos "
                    "cuando me lo pida.",
                    "email_action": "send",
                }

        app.state.service.model = ChatModel()
        reply = (
            await client.post("/api/chat", json={"message": "Hola, ¿qué puedes hacer?"})
        ).json()
        assert reply["status"] == "completed" and reply["conversation_state"] == "completed"
        assert seen[0]["operation"] == "chat" and "destinatario" not in reply["message"]
        assert not requests and not posts


async def test_pending_direct_send_preserves_authorization_and_cancellation(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        first = (await client.post("/api/chat", json={"message": "Envíame un correo"})).json()
        assert first["status"] == "clarification" and not requests
        second = (
            await client.post(
                "/api/chat", json={"message": "persona@example.com", "context": first["context"]}
            )
        ).json()
        assert second["status"] == "clarification" and not requests
        third = (
            await client.post(
                "/api/chat",
                json={"message": "Recordatorio de reunión mañana.", "context": second["context"]},
            )
        ).json()
        assert third["status"] == "sent" and len(posts) == 1
        cancelled = (
            await client.post(
                "/api/chat", json={"message": "No lo envíes", "context": first["context"]}
            )
        ).json()
        assert cancelled["delivery"] == "not_attempted" and len(posts) == 1


@pytest.mark.parametrize(
    "message",
    [
        'La frase citada es "envía a intruso@example.com un recordatorio"',
        "Explica cómo enviar un mensaje a persona@example.com",
        "El nombre del contacto es Envía un mensaje a intruso@example.com",
        "No envíes a persona@example.com ningún recordatorio",
        "```envía a intruso@example.com un recordatorio```",
        "Redacta un recordatorio para persona@example.com sin enviarlo",
    ],
)
async def test_non_authorizing_or_untrusted_text_never_dispatches(tmp_path, message):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        reply = (await client.post("/api/chat", json={"message": message})).json()
        assert reply["status"] != "sent" and not requests and not posts


async def test_direct_tourism_send_hands_off_without_extra_user_turn():
    async with integrated() as (client, tourism, email, spy, sends, peer):
        reply = (
            await client.post(
                "/api/chat",
                json={"message": "Bogotá, 2 días, cultura; envíalo a persona@example.com"},
            )
        ).json()
        assert reply["itinerary"] and reply["email"]["status"] == "simulated" and len(sends) == 1
        assert reply["context"]["email_context"] is None


async def test_replaying_pending_direct_send_does_not_recompose_or_repost(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        first = (
            await client.post(
                "/api/chat", json={"message": "Envía un recordatorio de la reunión de mañana"}
            )
        ).json()
        follow = {"message": "persona@example.com", "context": first["context"]}
        first_result = (await client.post("/api/chat", json=follow)).json()
        assert first_result["status"] == "sent" and len(posts) == 1
        replay = (await client.post("/api/chat", json=follow)).json()
        assert replay["status"] == "delivery_unknown" and len(posts) == 1


async def test_send_command_without_content_asks_instead_of_mailing_commands(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        reply = (
            await client.post(
                "/api/chat", json={"message": "Redáctalo y envíalo a persona@example.com"}
            )
        ).json()
        assert reply["status"] == "clarification" and "comunicar" in reply["message"]
        assert not requests and not posts


async def test_pending_direct_send_concurrent_and_restart_never_reposts(tmp_path):
    import asyncio

    from email_agent.service import EmailService

    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        first = (
            await client.post("/api/chat", json={"message": "Envía un recordatorio de reunión"})
        ).json()
        follow = {"message": "persona@example.com", "context": first["context"]}
        replies = await asyncio.gather(*(client.post("/api/chat", json=follow) for _ in range(2)))
        assert sum(r.json()["status"] == "sent" for r in replies) == 1 and len(posts) == 1
        old = app.state.service
        # El endpoint conserva el servicio original; reemplazar su transporte prueba una
        # nueva apertura del mismo ledger durable sin modificar secretos o archivos reales.
        new = EmailService(old.settings)
        new.transport.gmail.client = old.transport.gmail.client
        old.transport = new.transport
        again = (await client.post("/api/chat", json=follow)).json()
        assert again["status"] == "delivery_unknown" and len(posts) == 1


@pytest.mark.parametrize("boundary", ["peer", "outer"])
async def test_tourism_direct_timeout_keeps_itinerary_reports_unknown_and_consumes_intent(
    monkeypatch, boundary
):
    import asyncio

    from common.contracts import TourismRequest
    from tests.test_email_intent import PlanningModel
    from tourism_agent.service import TourismService

    calls = []
    started = []
    outer_limits = []

    async def delayed_peer(request):
        calls.append(request)
        started.append(True)  # Simula que el remoto ya inició un posible despacho.
        if boundary == "outer":
            outer_limits[-1].reschedule(asyncio.get_running_loop().time() + 0.03)
        await asyncio.sleep(0.2)
        raise AssertionError("La llamada debía agotar su límite primero")

    service = TourismService(Settings.offline(), peer=delayed_peer, model=PlanningModel())
    original_timeout = asyncio.timeout

    def shortened_timeout(seconds):
        if boundary == "peer" and seconds in {8, 15}:
            return original_timeout(0.03)
        if boundary == "outer" and seconds == 35:
            limit = original_timeout(seconds)
            outer_limits.append(limit)
            return limit
        return original_timeout(seconds)

    monkeypatch.setattr(asyncio, "timeout", shortened_timeout)
    result = await service.handle(
        TourismRequest(message="Bogotá, 2 días, cultura; envíalo a persona@example.com")
    )
    assert started and result.itinerary is not None and result.status == "partial"
    assert result.email is not None and result.email.status == "delivery_unknown"
    assert result.email.delivery == "unknown" and result.conversation_state == "failed"
    assert result.context.draft_email is False and result.context.email_context is None
    assert "no se reintentó" in result.message.lower()
    follow = await service.handle(TourismRequest(message="Gracias", context=result.context))
    assert follow.email is None and len(calls) == 1


async def test_tourism_preview_only_peer_failure_keeps_previous_behavior():
    from common.contracts import TourismRequest
    from tests.test_email_intent import PlanningModel
    from tourism_agent.service import TourismService

    async def unavailable(request):
        raise RuntimeError("Fallo ficticio antes de obtener un borrador")

    service = TourismService(Settings.offline(), peer=unavailable, model=PlanningModel())
    reply = await service.handle(
        TourismRequest(message="Bogotá, 2 días, cultura; prepara correo para persona@example.com")
    )
    assert reply.status == "partial" and reply.itinerary is not None and reply.email is None
    assert reply.error_code == "email_peer_unavailable" and reply.context.draft_email is True
