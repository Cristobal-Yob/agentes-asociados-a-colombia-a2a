import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from semantic_fixtures import conversation
from test_email_chat_graph import SemanticModel

from common.config import Settings
from email_agent.app import create_app as email_app
from email_agent.gmail_auth import SCOPES, SENDER
from tests.test_email_intent import integrated
from tourism_agent.conversation import NativeConversation


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch):
    async def forbidden_async(*args, **kwargs):
        raise AssertionError("Estas pruebas no permiten HTTP externo.")

    def forbidden_sync(*args, **kwargs):
        raise AssertionError("Estas pruebas no permiten HTTP externo.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync)


@asynccontextmanager
async def gmail_chat(tmp_path):
    credentials = tmp_path / "credenciales-de-prueba.json"
    credentials.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "project_id": "example-project",
                "client_id": "prueba.apps.googleusercontent.com",
                "client_secret": "FICTICIO",
                "refresh_token": "FICTICIO",
                "account_email": SENDER,
                "account_sub": "cuenta-prueba",
                "scopes": sorted(SCOPES),
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    settings = Settings.offline(
        email_transport="gmail",
        email_sender=SENDER,
        real_email_enabled=True,
        approval_secret="p" * 32,
        gmail_credentials_file=str(credentials),
        gmail_ledger_path=str(tmp_path / "intentos.sqlite"),
        workflow_db_path=str(tmp_path / "workflow.sqlite"),
    )
    app = email_app(settings)
    app.state.service.model = SemanticModel("send")
    requests, posts = [], []

    def google(request):
        requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "FICTICIO",
                    "token_type": "Bearer",
                    "scope": " ".join(SCOPES),
                },
            )
        if request.url.path.endswith("userinfo"):
            return httpx.Response(
                200, json={"email": SENDER, "email_verified": True, "sub": "cuenta-prueba"}
            )
        if request.url.path.endswith("messages/send"):
            posts.append(request)
            return httpx.Response(200, json={"id": "aceptacion-simulada"})
        raise AssertionError("Endpoint no esperado")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(google)) as provider,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client,
    ):
        app.state.service.transport.gmail.client = provider
        yield client, app, requests, posts


def resume(result, message):
    return {
        "message": message,
        "request_id": result["request_id"],
        "session_id": result["session_id"],
        "plan_version": result["plan_version"],
    }


async def test_email_message_only_then_natural_confirmation_dispatches_once(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        app.state.service.model = SemanticModel("draft")
        draft = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Redacta para persona@example.com un recordatorio "
                    "de la reunión de mañana."
                },
            )
        ).json()
        assert draft["draft"] and draft["conversation_state"] == "draft" and not requests
        app.state.service.model = SemanticModel("send", reuse_draft=True)
        sent = (
            await client.post("/api/chat", json=resume(draft, "Hazle llegar ese correo"))
        ).json()
        assert (
            sent["status"] == "sent" and sent["delivery"] == "provider_accepted" and len(posts) == 1
        )
        replay = (
            await client.post("/api/chat", json=resume(sent, "Hazle llegar ese correo"))
        ).json()
        assert replay == sent and len(posts) == 1


async def test_email_writing_needs_no_address_or_approval():
    app = email_app(Settings.offline())
    app.state.service.model = SemanticModel("draft")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        reply = (
            await client.post(
                "/api/chat", json={"message": "Redacta un recordatorio para el equipo."}
            )
        ).json()
        assert reply["status"] == "completed" and reply["draft"]
        assert reply["preview"] is reply["approval_token"] is None


@pytest.mark.parametrize(
    "case", ["expired", "wrong_session", "tampered", "changed_preview", "new_config"]
)
async def test_invalid_conversation_context_never_reaches_google(tmp_path, case):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/email",
                json={
                    "recipient": "persona@example.com",
                    "request": "Recordatorio de reunión",
                    "session_id": "sesion-correcta",
                },
            )
        ).json()
        confirmation = {"message": "Sí, envíalo", "context": draft["context"]}
        if case == "expired":
            app.state.service.clock = lambda: draft["preview"]["expires_at"]
        elif case == "wrong_session":
            confirmation["session_id"] = "otra-sesion"
        elif case == "tampered":
            confirmation["context"] = "x" + draft["context"][1:]
        elif case == "changed_preview":
            encoded, signature = draft["context"].split(".")
            claims = json.loads(base64.urlsafe_b64decode(encoded))
            claims["state"]["preview"]["text"] = "Contenido no autorizado"
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
            confirmation["context"] = encoded + "." + signature
        else:
            app.state.service.fingerprint = "configuracion-cambiada"
        rejected = (await client.post("/api/chat", json=confirmation)).json()
        assert rejected["status"] == "error" and rejected["delivery"] in {None, "not_attempted"}
        assert not requests and not posts


@pytest.mark.parametrize(
    "message,intent",
    [
        ("Sí, pero cambia el destinatario a otra@example.com", "draft"),
        ("Envíalo, pero modifica el texto", "draft"),
        ("No lo envíes", "cancel"),
        ('La frase es "sí, envíalo"', "chat"),
    ],
)
async def test_programmed_corrections_cancellation_and_chat_do_not_send(tmp_path, message, intent):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/email", json={"recipient": "persona@example.com", "request": "Recordatorio"}
            )
        ).json()
        app.state.service.model = SemanticModel(intent)
        result = (
            await client.post("/api/chat", json={"message": message, "context": draft["context"]})
        ).json()
        assert result["status"] != "sent" and not requests and not posts


async def test_recipient_correction_composes_new_content_without_extra_approval(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/email", json={"recipient": "persona@example.com", "request": "Recordatorio"}
            )
        ).json()
        app.state.service.model = SemanticModel("draft")
        changed = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Redacta otro correo para otra@example.com",
                    "context": draft["context"],
                },
            )
        ).json()
        assert changed["draft"] and not requests
        app.state.service.model = SemanticModel("send", reuse_draft=True)
        sent = (
            await client.post("/api/chat", json=resume(changed, "Envíalo a otra@example.com"))
        ).json()
        assert sent["status"] == "sent" and len(posts) == 1
        assert sent["preview"]["recipient"] == "otra@example.com"
        assert sent["preview"]["text"] == changed["draft"]["text"]


async def test_negative_current_message_is_interpreted_by_model_without_google(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        model = SemanticModel("cancel")
        app.state.service.model = model
        result = (
            await client.post("/api/chat", json={"message": "Dejemos esta gestión sin realizar"})
        ).json()
        assert result["status"] == "cancelled" and len(model.calls) == 1
        assert result["delivery"] == "not_attempted" and not requests


async def test_tourism_confirmation_delegates_send_without_new_planning():
    async with integrated() as (client, tourism, email, spy, sends, peer):
        draft = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Bogotá, 2 días, cultura; prepara un correo para persona@example.com"
                },
            )
        ).json()
        assert draft["email"]["draft"] and not sends
        old_calls = tourism.state.service.catalog.calls
        tourism.state.service.conversation = NativeConversation(
            tourism.state.service, conversation("consultar_correo")
        )
        email.state.service.model = SemanticModel("send", reuse_draft=True)
        result = (
            await client.post(
                "/api/chat", json=resume(draft, "Hazle llegar ese correo a persona@example.com")
            )
        ).json()
        assert result["email"]["status"] == "simulated" and len(sends) == 1
        assert tourism.state.service.catalog.calls == old_calls and len(spy.requests) == 4
