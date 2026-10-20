import httpx
import pytest

from common.config import Settings
from tourism_agent.app import create_app


@pytest.mark.parametrize("key", [None, "wrong", "email-key-crossed"])
async def test_business_and_a2a_fail_closed_without_correct_service_key(key):
    settings = Settings.offline(auth_required=True, service_api_key="tourism-key")
    app = create_app(settings)
    headers = {"X-API-Key": key} if key else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as client:
        for path in ("/api/chat", "/a2a"):
            assert (
                await client.post(path, headers=headers, json={"message": "Hola"})
            ).status_code == 401
        assert (
            await client.get("/api/requests/unknown?session_id=s", headers=headers)
        ).status_code == 401


async def test_public_metadata_swagger_and_card_describe_real_authentication():
    app = create_app(Settings.offline(auth_required=True, service_api_key="tourism-key"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as client:
        for path in ("/health", "/docs", "/openapi.json", "/.well-known/agent-card.json"):
            assert (await client.get(path)).status_code == 200
        schema = (await client.get("/openapi.json")).json()
        assert schema["paths"]["/api/chat"]["post"]["security"]
        assert any(
            value.get("name") == "X-API-Key"
            for value in schema["components"]["securitySchemes"].values()
        )
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert (
            card["securityRequirements"]
            and card["securitySchemes"]["service_key"]["apiKeySecurityScheme"]["name"]
            == "X-API-Key"
        )
        result = await client.post(
            "/api/chat", headers={"X-API-Key": "tourism-key"}, json={"message": "Hola"}
        )
        assert result.status_code == 200


async def test_missing_runtime_auth_configuration_is_503_never_open():
    app = create_app(Settings.offline(auth_required=True))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as client:
        assert (await client.post("/api/chat", json={"message": "Hola"})).status_code == 503


def test_keys_are_hidden_from_settings_representation():
    settings = Settings.offline(
        service_api_key="SENSITIVE_INBOUND", email_peer_api_key="SENSITIVE_PEER"
    )
    assert "SENSITIVE" not in repr(settings)


async def test_native_sdk_sends_target_key_without_removing_iam_header():
    from common.a2a_transport import send_typed

    app = create_app(Settings.offline(auth_required=True, service_api_key="tourism-key"))
    seen = []

    async def observe(request):
        seen.append((request.headers.get("x-api-key"), request.headers.get("authorization")))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer SYNTHETIC_IAM"},
        event_hooks={"request": [observe]},
    ) as client:
        result, task = await send_typed(
            "http://127.0.0.1:8001", {}, api_key="tourism-key", http_client=client
        )
    assert result["status"] == "clarification" and task.id
    assert all(key == "tourism-key" and iam == "Bearer SYNTHETIC_IAM" for key, iam in seen)
