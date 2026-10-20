import httpx
import pytest
from a2a.types import TaskState

from common.a2a_transport import send_typed
from common.config import Settings
from email_agent.app import create_app as email_app
from tourism_agent.app import create_app as tourism_app


@pytest.mark.asyncio
async def test_native_sdk_task_and_conversation_context():
    app = tourism_app(Settings.offline(public_url="http://127.0.0.1:8001"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as client:
        result, task = await send_typed("http://127.0.0.1:8001", {}, http_client=client)
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert result["missing_field"] == "city"
        second, continued = await send_typed(
            "http://127.0.0.1:8001",
            {"context": {**result["context"], "city": "Bogota"}},
            task_id=task.id,
            context_id=task.context_id,
            http_client=client,
        )
        assert continued.context_id == task.context_id
        assert second["missing_field"] == "days"


@pytest.mark.asyncio
async def test_invalid_native_payload_is_failed_task():
    app = email_app(Settings.offline(public_url="http://127.0.0.1:8002"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as client:
        result, task = await send_typed(
            "http://127.0.0.1:8002", {"unknown": True}, http_client=client
        )
        assert task.status.state == TaskState.TASK_STATE_FAILED
        assert result["error_code"] == "invalid_payload"


@pytest.mark.asyncio
async def test_peer_card_ssrf_is_rejected_before_send():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "name": "bad",
                "description": "bad",
                "version": "1",
                "supportedInterfaces": [
                    {
                        "url": "http://169.254.169.254/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="lista permitida"):
            await send_typed("http://127.0.0.1:8002", {}, http_client=client)
    assert len(requests) == 1
