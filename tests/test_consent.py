import time

import pytest

from common.config import Settings
from common.contracts import EmailRequest
from email_agent.service import EmailService


@pytest.mark.asyncio
async def test_send_requires_explicit_confirmation_and_exact_preview():
    service = EmailService(Settings.offline())
    preview = await service.handle(
        EmailRequest(
            session_id="lesson", recipient="student@example.com", request="Draft a welcome note"
        )
    )
    assert preview.status == "preview"
    rejected = await service.handle(
        EmailRequest(
            session_id="lesson",
            action="send",
            preview=preview.preview,
            approval_token=preview.approval_token,
        )
    )
    assert rejected.status == "error"
    sent = await service.handle(
        EmailRequest(
            session_id="lesson",
            action="send",
            preview=preview.preview,
            approval_token=preview.approval_token,
            confirmed=True,
        )
    )
    assert sent.status == "simulated"
    assert sent.delivery == "simulated_only"


@pytest.mark.asyncio
async def test_changed_recipient_or_expiry_fails_closed():
    service = EmailService(Settings.offline())
    result = await service.handle(
        EmailRequest(session_id="lesson", recipient="student@example.com", request="Hello")
    )
    changed = result.preview.model_copy(update={"recipient": "other@example.com"})
    request = EmailRequest(
        session_id="lesson",
        action="send",
        preview=changed,
        approval_token=result.approval_token,
        confirmed=True,
    )
    assert (await service.handle(request)).status == "error"
    service.clock = lambda: time.time() + 301
    request.preview = result.preview
    assert (await service.handle(request)).status == "error"
