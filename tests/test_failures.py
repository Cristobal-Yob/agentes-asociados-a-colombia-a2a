from dataclasses import replace

import httpx
import pytest

from common.config import Settings
from common.contracts import EmailRequest, TourismRequest, TripContext
from email_agent.service import EmailService
from email_agent.transport import MailTransport
from tourism_agent.service import TourismService


@pytest.mark.asyncio
async def test_send_deadline_is_unknown_not_failure_claim():
    service = EmailService(Settings.offline())

    async def timeout(*args):
        raise TimeoutError

    service.runtime.run = timeout
    result = await service.handle(EmailRequest(action="send", confirmed=True))
    assert result.status == "delivery_unknown"


@pytest.mark.asyncio
async def test_capacity_rejection_proves_no_send_was_attempted():
    class CountingTransport:
        calls = 0

        async def send(self, preview):
            self.calls += 1
            raise AssertionError("Capacity rejection must never dispatch")

    transport = CountingTransport()
    service = EmailService(Settings.offline(), transport=transport)
    draft = await service.handle(EmailRequest(recipient="student@example.com", request="Hello"))
    send = EmailRequest(
        action="send", confirmed=True, preview=draft.preview, approval_token=draft.approval_token
    )
    for _ in range(8):
        await service.runtime.slots.acquire()
    try:
        result = await service.handle(send)
    finally:
        for _ in range(8):
            service.runtime.slots.release()
    assert transport.calls == 0
    assert result.status == "error"
    assert result.error_code == "capacity_exceeded"
    assert result.delivery == "not_attempted"


@pytest.mark.asyncio
async def test_post_dispatch_timeout_is_unknown_without_automatic_retry():
    class TimeoutTransport:
        calls = 0

        async def send(self, preview):
            self.calls += 1
            raise TimeoutError

    transport = TimeoutTransport()
    service = EmailService(Settings.offline(), transport=transport)
    draft = await service.handle(EmailRequest(recipient="student@example.com", request="Hello"))
    result = await service.handle(
        EmailRequest(
            action="send",
            confirmed=True,
            preview=draft.preview,
            approval_token=draft.approval_token,
        )
    )
    assert result.status == "delivery_unknown"
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_unverified_domain_never_posts_and_provider_timeout_is_unknown():
    settings = Settings.offline(
        email_transport="resend",
        approval_secret="s" * 32,
        resend_api_key="test-key",
        email_sender="teacher@school.example",
        real_email_enabled=True,
    )
    requests = []
    verified = False

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "school.example", "status": "verified" if verified else "pending"}
                    ]
                },
            )
        raise httpx.ReadTimeout("Timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = EmailService(settings, transport=MailTransport(settings, client))
        draft = await service.handle(EmailRequest(recipient="student@example.com", request="Hello"))
        send = EmailRequest(
            action="send",
            confirmed=True,
            approval_token=draft.approval_token,
            preview=draft.preview,
        )
        assert (await service.handle(send)).error_code == "unverified_sender"
        assert len(requests) == 1
        verified = True
        assert (await service.handle(send)).status == "delivery_unknown"
        first_key = requests[-1].headers["Idempotency-Key"]
        assert (await service.handle(send)).status == "delivery_unknown"
        assert requests[-1].headers["Idempotency-Key"] == first_key


@pytest.mark.asyncio
async def test_changed_session_body_transport_and_config_fail_closed():
    settings = Settings.offline(approval_secret="s" * 32)
    service = EmailService(settings)
    result = await service.handle(
        EmailRequest(session_id="a", recipient="student@example.com", request="Hello")
    )
    for changes in [
        {"text": "Changed"},
        {"subject": "Changed"},
        {"transport": "resend"},
        {"sender": "other@example.com"},
    ]:
        send = EmailRequest(
            action="send",
            session_id="a",
            confirmed=True,
            preview=result.preview.model_copy(update=changes),
            approval_token=result.approval_token,
        )
        assert (await service.handle(send)).error_code == "approval_invalid"
    send = EmailRequest(
        action="send",
        session_id="b",
        confirmed=True,
        preview=result.preview,
        approval_token=result.approval_token,
    )
    assert (await service.handle(send)).error_code == "approval_invalid"
    different = EmailService(replace(settings, email_sender="changed@example.com"))
    send.session_id = "a"
    assert (await different.handle(send)).error_code == "approval_invalid"


@pytest.mark.asyncio
async def test_peer_failure_preserves_itinerary_and_untrusted_model_ids_fail():
    async def unavailable(request):
        raise httpx.ConnectError("Unavailable")

    context = TripContext(city="Bogota", days=2, interests=["culture"])
    service = TourismService(Settings.offline(), peer=unavailable)
    result = await service.handle(
        TourismRequest(context=context, draft_email=True, recipient="student@example.com")
    )
    assert result.status == "partial"
    assert result.itinerary is not None

    class InventedModel:
        async def invoke(self, payload):
            return {"ids": ["invented-paid-tour"]}

    service = TourismService(Settings.offline(), model=InventedModel())
    result = await service.handle(TourismRequest(context=context))
    assert result.status == "error"
    assert result.itinerary is None
