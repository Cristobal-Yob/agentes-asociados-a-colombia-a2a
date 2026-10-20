from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from common.config import Settings, checked_base
from common.contracts import EmailRequest
from email_agent.app import create_app
from email_agent.service import EmailService


def test_boolean_consent_is_not_coerced_and_urls_are_allowlisted():
    with pytest.raises(ValidationError):
        EmailRequest(confirmed="true")
    for url in [
        "http://169.254.169.254",
        "https://user:pass@host",
        "https://host/path",
        "https://host?url=x",
    ]:
        with pytest.raises(ValueError):
            checked_base(url)
    with pytest.raises(ValueError):
        EmailService(replace(Settings.offline(), email_transport="resend"))


@pytest.mark.asyncio
async def test_oversized_body_and_log_redaction(caplog):
    app = create_app(Settings.offline(public_url="http://127.0.0.1:8002"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.post("/api/email", content="x" * 32769)).status_code == 413
        with caplog.at_level("INFO", logger="classroom"):
            result = await client.post(
                "/api/email",
                json={"recipient": "private@example.com", "request": "SECRET_CONTENT_MARKER"},
            )
        assert result.status_code == 200
        assert "private@example.com" not in caplog.text
        assert "SECRET_CONTENT_MARKER" not in caplog.text
        assert result.json()["approval_token"] not in caplog.text


@pytest.mark.asyncio
async def test_replay_survives_restart_only_with_persistent_key():
    settings = Settings.offline(approval_secret="classroom-demo-secret-at-least-32-bytes")
    first = EmailService(settings)
    result = await first.handle(EmailRequest(recipient="student@example.com", request="Hello"))
    request = EmailRequest(
        action="send", confirmed=True, preview=result.preview, approval_token=result.approval_token
    )
    second = EmailService(settings)
    assert (await second.handle(request)).status == "simulated"
    assert (await second.handle(request)).status == "simulated"
    assert (await EmailService(Settings.offline()).handle(request)).error_code == "approval_invalid"
