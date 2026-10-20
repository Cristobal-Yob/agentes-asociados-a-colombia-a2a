"""Hermetic UI boundary tests. No Google, agent or email requests are made."""

import asyncio
import copy
import hashlib
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_ui.app import create_app
from demo_ui.config import Config


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    async def atomic(self, key, mutate):
        with self.lock:
            row = copy.deepcopy(self.rows.get(key, {}))
            result = mutate(row)
            self.rows[key] = row
            return copy.deepcopy(result)


class FakeOIDC:
    email = "owner@example.com"
    nonce_override = None
    verified = True
    subject = "owner-google-sub"

    async def exchange(self, code, verifier, nonce):
        assert code == "test-code" and len(verifier) >= 43
        return {
            "sub": self.subject,
            "email": self.email,
            "email_verified": self.verified,
            "nonce": self.nonce_override or nonce,
        }


class FakeAgents:
    def __init__(self):
        self.calls = []
        self.unknown = False
        self.recovered = False

    async def post(self, agent, payload):
        self.calls.append((agent, copy.deepcopy(payload)))
        if payload.get("action") == "send":
            if self.unknown:
                raise httpx.ReadTimeout("offline timeout")
            return {"status": "sent", "delivery": "provider_accepted", "message": "Aceptado."}
        preview = {
            "draft_id": "signed-draft",
            "session_id": "child-session",
            "plan_version": 7,
            "recipient": "person@example.com",
            "subject": "Tu viaje",
            "text": "Contenido",
            "sender": "sender@example.com",
            "transport": "gmail",
            "expires_at": int(time.time()) + 300,
        }
        email = {
            "status": "preview",
            "request_id": "child-request",
            "session_id": "child-session",
            "plan_version": 7,
            "preview": preview,
            "approval_token": "private-signature",
        }
        if agent == "email":
            return email
        return {
            "status": "completed",
            "itinerary": {
                "city": "Medellín",
                "summary": "Tres días.",
                "days": [{"day": 1, "activities": ["Museo"]}],
                "warnings": ["Horarios no verificados."],
                "sources": [],
            },
            "email": email if payload.get("draft_email") else None,
        }

    async def lookup(self, agent, request_id, session_id):
        assert agent == "email" and request_id == "child-request"
        assert session_id == "child-session"
        if not self.recovered:
            return {"state": "unknown", "operations": []}
        send = next(p for _, p in self.calls if p.get("action") == "send")
        return {
            "state": "completed",
            "operations": [
                {
                    "operation_id": send["operation_id"],
                    "state": "completed",
                    "receipt": {"status": "sent", "delivery": "provider_accepted"},
                }
            ],
        }


def configured():
    return Config(
        origin="https://colombia.example",
        client_id="test.apps.googleusercontent.com",
        client_secret="test-only-secret",
        project="example-project",
        database="colombia-ui-test",
        itinerary_key="i" * 32,
        email_key="e" * 32,
        owner_email="owner@example.com",
        itinerary_url="https://itinerary.example.invalid",
        email_url="https://email.example.invalid",
        hub_return="https://hub.example.invalid/classroom",
        sender_email="sender@example.com",
    )


@pytest.fixture
def rig():
    store, oidc, agents = MemoryStore(), FakeOIDC(), FakeAgents()
    app = create_app(configured(), store=store, oidc=oidc, agents=agents)
    with TestClient(app, base_url="https://colombia.example") as client:
        yield client, store, oidc, agents


def login(client):
    start = client.get("/auth/login", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    callback = client.get(
        "/auth/callback",
        params={"state": query["state"][0], "code": "test-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    session = client.get("/api/session").json()
    headers = {"Origin": "https://colombia.example", "X-CSRF-Token": session["csrf"]}
    return headers, query["state"][0]


def draft(client, headers):
    response = client.post(
        "/api/drafts",
        headers=headers,
        json={
            "recipient": "person@example.com",
            "request": "Envía este texto, pero solo redacta aquí",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_missing_configuration_fails_closed(monkeypatch):
    for name in Config.ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_app())
    assert client.get("/health").status_code == 503
    assert client.get("/auth/login").status_code == 503
    assert client.get("/api/session").status_code == 503
    assert "itinerary_key" not in client.get("/api/config").text


def test_login_state_binding_replay_and_logout(rig):
    client, _, _, _ = rig
    headers, state = login(client)
    cookie = next(
        value for key, value in client.cookies.items() if key == "__Host-colombia-session"
    )
    assert len(cookie) >= 43
    assert (
        client.get(
            "/auth/callback", params={"state": state, "code": "test-code"}, follow_redirects=False
        ).status_code
        == 400
    )
    assert client.post("/auth/logout", headers=headers, json={}).status_code == 200
    assert client.get("/api/session").status_code == 401


@pytest.mark.parametrize("fault", ["nonce", "owner", "verified", "binding"])
def test_oidc_rejects_invalid_identity(rig, fault):
    client, _, oidc, _ = rig
    start = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    if fault == "nonce":
        oidc.nonce_override = "wrong"
    elif fault == "owner":
        oidc.email = "student@example.com"
    elif fault == "verified":
        oidc.verified = False
    else:
        client.cookies.clear()
    assert client.get(
        "/auth/callback", params={"state": state, "code": "test-code"}, follow_redirects=False
    ).status_code in {400, 403}
    assert client.get("/api/session").status_code == 401


def test_csrf_origin_unknown_fields_and_anonymous_access(rig):
    client, _, _, _ = rig
    assert client.post("/api/drafts", json={}).status_code == 401
    headers, _ = login(client)
    assert client.post("/api/drafts", json={}).status_code == 403
    assert (
        client.post(
            "/api/drafts", headers={**headers, "Origin": "https://evil.example"}, json={}
        ).status_code
        == 403
    )
    assert client.post("/api/drafts", headers=headers, json={"action": "send"}).status_code == 422


def test_typed_itinerary_and_independent_draft_cannot_send(rig):
    client, _, _, agents = rig
    headers, _ = login(client)
    result = client.post(
        "/api/itineraries",
        headers=headers,
        json={"city": "Medellín", "days": 3, "interests": ["culture"]},
    )
    assert result.status_code == 200
    payload = agents.calls[-1][1]
    assert payload["message"] == "" and payload["draft_email"] is False
    assert payload["context"]["agenda_requested"] is False
    generated = draft(client, headers)
    assert agents.calls[-1][1]["action"] == "draft"
    assert "message" not in agents.calls[-1][1]
    assert "private-signature" not in str(generated)
    assert "approval_token" not in str(generated)
    assert len({p["operation_id"] for _, p in agents.calls}) == 2


def test_nested_a2a_ids_preserved_and_send_exactly_once(rig):
    client, _, _, agents = rig
    headers, _ = login(client)
    result = client.post(
        "/api/itineraries",
        headers=headers,
        json={
            "city": "Medellín",
            "days": 3,
            "interests": [],
            "prepare_email": True,
            "recipient": "person@example.com",
        },
    ).json()
    draft_id = result["draft"]["id"]
    first = client.post(f"/api/drafts/{draft_id}/send", headers=headers, json={})
    second = client.post(f"/api/drafts/{draft_id}/send", headers=headers, json={})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    sent = [p for _, p in agents.calls if p.get("action") == "send"]
    assert len(sent) == 1
    assert sent[0]["request_id"] == "child-request"
    assert sent[0]["session_id"] == "child-session" and sent[0]["plan_version"] == 7
    assert sent[0]["preview"]["text"] == "Contenido"
    assert sent[0]["approval_token"] == "private-signature" and sent[0]["confirmed"] is True


def test_unknown_delivery_blocks_bypass_and_recovers_without_post_retry(rig):
    client, _, _, agents = rig
    headers, _ = login(client)
    generated = draft(client, headers)
    agents.unknown = True
    result = client.post(
        f"/api/drafts/{generated['draft']['id']}/send", headers=headers, json={}
    ).json()
    assert result["state"] == "unknown"
    assert (
        client.post(
            "/api/drafts",
            headers=headers,
            json={"recipient": "person@example.com", "request": "Otro"},
        ).status_code
        == 409
    )
    assert client.get(f"/api/operations/{result['id']}").json()["state"] == "unknown"
    agents.recovered = True
    recovered = client.get(f"/api/operations/{result['id']}").json()
    assert recovered["state"] == "completed" and recovered["receipt"]["status"] == "sent"
    assert len([p for _, p in agents.calls if p.get("action") == "send"]) == 1


def test_subject_isolation_and_expired_preview(rig):
    client, store, _, agents = rig
    headers, _ = login(client)
    generated = draft(client, headers)
    workspace = store.rows["user:" + hashlib.sha256(b"owner-google-sub").hexdigest()]
    workspace["drafts"][generated["draft"]["id"]]["email"]["preview"]["expires_at"] = 1
    assert (
        client.post(
            f"/api/drafts/{generated['draft']['id']}/send", headers=headers, json={}
        ).status_code
        == 409
    )
    assert len(agents.calls) == 1
    assert client.get("/api/operations/not-owned").status_code == 404


def test_atomic_send_reservation_under_concurrency(rig):
    client, store, _, agents = rig
    headers, _ = login(client)
    generated = draft(client, headers)
    app = client.app

    async def invoke():
        cookies = dict(client.cookies)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://colombia.example",
            cookies=cookies,
        ) as browser:
            return await asyncio.gather(
                *[
                    browser.post(
                        f"/api/drafts/{generated['draft']['id']}/send", headers=headers, json={}
                    )
                    for _ in range(5)
                ]
            )

    results = asyncio.run(invoke())
    assert all(r.status_code == 200 for r in results)
    assert len([p for _, p in agents.calls if p.get("action") == "send"]) == 1


def test_static_assets_are_allowlisted_and_security_headers_exist(rig):
    client, _, _, _ = rig
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../config.py").status_code == 404
    assert client.get("/static/config.py").status_code == 404


def test_session_expiry_cookie_attributes_and_subject_isolation(rig):
    client, store, oidc, _ = rig
    headers, _ = login(client)
    original = draft(client, headers)
    oidc.subject = "different-google-sub"
    other_headers, _ = login(client)
    assert client.get(f"/api/operations/{original['id']}").status_code == 404
    assert (
        client.post(
            f"/api/drafts/{original['draft']['id']}/send", headers=other_headers, json={}
        ).status_code
        == 404
    )
    token = client.cookies.get("__Host-colombia-session")
    store.rows["session:" + hashlib.sha256(token.encode()).hexdigest()]["expires"] = 1
    assert client.get("/api/session").status_code == 401
    start = client.get("/auth/login", follow_redirects=False)
    cookie = start.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["scope"] == ["openid email"] and query["code_challenge_method"] == ["S256"]
    assert "gmail" not in start.headers["location"]


def test_expired_login_body_limit_and_rate_limit(rig):
    client, store, _, _ = rig
    start = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    store.rows["login:" + hashlib.sha256(state.encode()).hexdigest()]["expires"] = 1
    assert (
        client.get("/auth/callback", params={"state": state, "code": "test-code"}).status_code
        == 400
    )
    headers, _ = login(client)
    assert (
        client.post(
            "/api/drafts",
            headers={**headers, "Content-Type": "application/json"},
            content="x" * 16_001,
        ).status_code
        == 413
    )
    owner = "user:" + hashlib.sha256(b"owner-google-sub").hexdigest()
    store.rows[owner] = {"recent": [time.time()] * 10}
    response = client.post(
        "/api/drafts", headers=headers, json={"recipient": "person@example.com", "request": "Test"}
    )
    assert response.status_code == 429


def test_old_or_pending_receipt_does_not_unlock_unknown_send(rig):
    client, _, _, agents = rig
    headers, _ = login(client)
    generated = draft(client, headers)
    agents.unknown = True
    sent = client.post(
        f"/api/drafts/{generated['draft']['id']}/send", headers=headers, json={}
    ).json()

    async def stale(agent, request_id, session_id):
        payload = next(p for _, p in agents.calls if p.get("action") == "send")
        return {
            "state": "unknown",
            "response": {"status": "preview"},
            "operations": [
                {
                    "operation_id": payload["operation_id"],
                    "state": "unknown",
                    "receipt": {"status": "sent", "delivery": "provider_accepted"},
                }
            ],
        }

    agents.lookup = stale
    assert client.get(f"/api/operations/{sent['id']}").json()["state"] == "unknown"
    duplicate = client.post(
        f"/api/drafts/{generated['draft']['id']}/send", headers=headers, json={}
    ).json()
    assert duplicate["id"] == sent["id"]
    assert len([p for _, p in agents.calls if p.get("action") == "send"]) == 1


@pytest.mark.asyncio
async def test_real_oidc_adapter_uses_fixed_endpoint_pkce_and_exact_audience(monkeypatch):
    from demo_ui.auth import GoogleOIDC

    captured = {}
    original_client = httpx.AsyncClient

    def transport(request):
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        captured["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"id_token": "fixture-token"})

    monkeypatch.setattr(
        "demo_ui.auth.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )

    def verify(token, request, audience):
        captured["audience"] = audience
        assert token == "fixture-token"
        return {"sub": "fixture-sub", "azp": audience}

    monkeypatch.setattr("demo_ui.auth.id_token.verify_oauth2_token", verify)
    await GoogleOIDC(configured()).exchange("fixture-code", "pkce-verifier", "nonce")
    assert captured["audience"] == configured().client_id
    assert captured["form"]["code_verifier"] == ["pkce-verifier"]
    assert captured["form"]["redirect_uri"] == ["https://colombia.example/auth/callback"]
