import asyncio
from dataclasses import replace

import httpx

from common.config import Settings
from common.contracts import EmailRequest
from email_agent.service import EmailService
from email_agent.transport import MailTransport


async def test_native_idempotency_key_survives_parallel_replicas():
    settings = Settings.offline(
        email_transport="resend",
        approval_secret="s" * 32,
        resend_api_key="MOCK_PROVIDER_KEY",
        email_sender="Teacher <teacher@school.example>",
        real_email_enabled=True,
    )
    keys = []

    def provider(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": [{"name": "school.example", "status": "verified"}]}
            )
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json={"id": "mock-accepted-id"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        first = EmailService(settings, transport=MailTransport(settings, client))
        second = EmailService(settings, transport=MailTransport(settings, client))
        draft = await first.handle(EmailRequest(recipient="student@example.com", request="Hello"))
        request = EmailRequest(
            action="send",
            preview=draft.preview,
            approval_token=draft.approval_token,
            confirmed=True,
        )
        results = await asyncio.gather(first.handle(request), second.handle(request))
        assert all(result.delivery == "provider_accepted" for result in results)
        assert len(keys) == 2 and len(set(keys)) == 1
        # The mock checks key propagation, not live provider exactly-once delivery.


async def test_wrong_domain_or_opt_in_disabled_never_sends():
    settings = Settings.offline(
        email_transport="resend",
        approval_secret="s" * 32,
        resend_api_key="MOCK_PROVIDER_KEY",
        email_sender="teacher@school.example",
        real_email_enabled=True,
    )
    posted = []

    def provider(request):
        if request.method == "POST":
            posted.append(request)
        return httpx.Response(
            200, json={"data": [{"name": "other.school.example", "status": "verified"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service = EmailService(settings, transport=MailTransport(settings, client))
        draft = await service.handle(EmailRequest(recipient="student@example.com", request="Hello"))
        request = EmailRequest(
            action="send",
            preview=draft.preview,
            approval_token=draft.approval_token,
            confirmed=True,
        )
        assert (await service.handle(request)).error_code == "unverified_sender"
        assert not posted
        disabled = MailTransport(replace(settings, real_email_enabled=False), client)
        assert (await disabled.send(draft.preview)).error_code == "provider_config"
        assert not posted
