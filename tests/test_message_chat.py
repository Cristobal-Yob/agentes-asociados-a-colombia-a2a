import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from common.config import Settings
from email_agent.app import create_app as email_app
from email_agent.gmail_auth import SCOPES, SENDER
from tests.test_email_intent import integrated


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
    )
    app = email_app(settings)
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


async def test_email_message_only_then_natural_confirmation_dispatches_once(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Redacta para persona@example.com un recordatorio "
                    "de la reunión de mañana."
                },
            )
        ).json()
        assert draft["status"] == "preview" and draft["context"]
        assert draft["conversation_state"] == "awaiting_confirmation"
        assert not requests
        confirmation = {"message": "Sí, envíalo", "context": draft["context"]}
        sent = (await client.post("/api/chat", json=confirmation)).json()
        assert sent["status"] == "sent" and sent["delivery"] == "provider_accepted"
        assert sent["conversation_state"] == "sent" and len(posts) == 1
        replay = (await client.post("/api/chat", json=confirmation)).json()
        assert replay["delivery"] == "provider_accepted" and len(posts) == 1


async def test_email_asks_one_missing_field_and_preserves_general_message():
    app = email_app(Settings.offline())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        first = (await client.post("/api/chat", json={"message": "Redacta un correo"})).json()
        assert first["status"] == "clarification" and "destinatario" in first["message"]
        second = (
            await client.post(
                "/api/chat", json={"message": "persona@example.com", "context": first["context"]}
            )
        ).json()
        assert second["status"] == "clarification" and "comunicar" in second["message"]
        third = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Recordatorio de la reunión de trabajo.",
                    "context": second["context"],
                },
            )
        ).json()
        assert third["status"] == "preview"
        assert third["preview"]["recipient"] == "persona@example.com"
        assert third["preview"]["text"] == "Recordatorio de la reunión de trabajo."


@pytest.mark.parametrize(
    "case", ["expired", "wrong_session", "tampered", "changed_preview", "new_config"]
)
async def test_invalid_conversation_context_never_reaches_google(tmp_path, case):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Redacta para persona@example.com un recordatorio de reunión.",
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
            claims["state"]["preview"]["text"] = "Contenido no aprobado"
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
            confirmation["context"] = encoded + "." + signature
        else:
            app.state.service.fingerprint = "configuracion-cambiada"
        rejected = (await client.post("/api/chat", json=confirmation)).json()
        assert rejected["status"] == "error" and rejected["delivery"] == "not_attempted"
        assert not requests and not posts


@pytest.mark.parametrize(
    "message",
    [
        "Sí, pero cambia el destinatario a otra@example.com",
        "Envíalo, pero modifica el texto",
        "No lo envíes",
        'La frase es "sí, envíalo"',
    ],
)
async def test_corrections_negation_and_quoted_confirmation_do_not_send(tmp_path, message):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/chat",
                json={"message": "Redacta para persona@example.com un recordatorio de reunión."},
            )
        ).json()
        result = (
            await client.post("/api/chat", json={"message": message, "context": draft["context"]})
        ).json()
        assert result["status"] != "sent" and not requests and not posts


async def test_recipient_correction_requires_new_preview_confirmation(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):
        draft = (
            await client.post(
                "/api/chat",
                json={"message": "Redacta para persona@example.com un recordatorio de reunión."},
            )
        ).json()
        changed = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Sí, pero cambia el destinatario a otra@example.com",
                    "context": draft["context"],
                },
            )
        ).json()
        assert changed["status"] == "preview" and changed["context"] != draft["context"]
        assert changed["preview"]["recipient"] == "otra@example.com" and not requests
        sent = (
            await client.post(
                "/api/chat", json={"message": "Sí, envíalo", "context": changed["context"]}
            )
        ).json()
        assert sent["status"] == "sent" and len(posts) == 1


async def test_first_negative_message_never_calls_model_or_google(tmp_path):
    async with gmail_chat(tmp_path) as (client, app, requests, posts):

        class ForbiddenModel:
            async def invoke(self, payload):
                raise AssertionError("Una negación no debe consultar el modelo.")

        app.state.service.model = ForbiddenModel()
        result = (
            await client.post(
                "/api/chat",
                json={
                    "message": "No envíes un correo a persona@example.com "
                    "con recordatorio de reunión."
                },
            )
        ).json()
        assert result["delivery"] == "not_attempted" and not requests


async def test_tourism_confirmation_delegates_send_without_new_planning():
    async with integrated() as (client, tourism, email, spy, sends, peer):
        draft = (
            await client.post(
                "/api/chat",
                json={
                    "message": "Bogotá, 2 días, cultura; prepáralo por correo "
                    "para persona@example.com"
                },
            )
        ).json()
        assert draft["context"].get("email_context") and not sends
        old_calls = tourism.state.service.catalog.calls
        result = (
            await client.post(
                "/api/chat", json={"message": "Sí, envíalo", "context": draft["context"]}
            )
        ).json()
        assert result["email"]["status"] == "simulated" and len(sends) == 1
        assert tourism.state.service.catalog.calls == old_calls
        assert len(spy.requests) == 4
