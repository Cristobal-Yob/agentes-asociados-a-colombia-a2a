import asyncio
import base64
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from common.config import Settings
from common.contracts import EmailRequest
from email_agent.dispatch_ledger import GcsLedger, SqliteLedger
from email_agent.gmail_auth import SCOPES, SENDER, OAuthError, load_credentials
from email_agent.service import EmailService
from email_agent.transport import MailTransport


@pytest.fixture
def gmail_settings(tmp_path):
    credentials = {
        "type": "authorized_user",
        "project_id": "example-project",
        "client_id": "test-client.apps.googleusercontent.com",
        "client_secret": "TEST_SECRET",
        "refresh_token": "TEST_REFRESH",
        "account_email": SENDER,
        "account_sub": "selected-sub",
        "scopes": sorted(SCOPES),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    path = tmp_path / "gmail.json"
    path.write_text(json.dumps(credentials), encoding="utf-8")
    return Settings.offline(
        **{
            "email_transport": "gmail",
            "email_sender": SENDER,
            "real_email_enabled": True,
            "approval_secret": "s" * 32,
            "gmail_credentials_file": str(path),
            "gmail_ledger": "sqlite",
            "gmail_ledger_path": str(tmp_path / "dispatch.sqlite"),
            "gmail_ledger_bucket": "",
        }
    )


class GoogleMock:
    def __init__(self, identity=None, timeout=False, auth_failure=False, scopes=SCOPES):
        self.identity = identity or {"email": SENDER, "email_verified": True, "sub": "selected-sub"}
        self.timeout = timeout
        self.auth_failure = auth_failure
        self.scopes = scopes
        self.sends = []
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/token":
            if self.auth_failure:
                return httpx.Response(400, json={"error": "SECRET_PROVIDER_ERROR"})
            return httpx.Response(
                200,
                json={
                    "access_token": "TEST_ACCESS",
                    "token_type": "Bearer",
                    "scope": " ".join(self.scopes),
                },
            )
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json=self.identity)
        if request.url.path.endswith("messages/send"):
            self.sends.append(json.loads(request.content))
            if self.timeout:
                raise httpx.ReadTimeout("No delivery assumption")
            return httpx.Response(200, json={"id": "known-provider-id"})
        raise AssertionError("Unexpected endpoint")


async def prepare(settings, client, clock=None):
    transport = MailTransport(settings, client)
    service = EmailService(settings, transport=transport)
    if clock is not None:
        service.clock = clock
    draft = await service.handle(
        EmailRequest(
            session_id="gmail-lesson", recipient="student@example.com", request="Exact preview body"
        )
    )
    assert draft.status == "preview"
    send = EmailRequest(
        action="send",
        session_id="gmail-lesson",
        confirmed=True,
        preview=draft.preview,
        approval_token=draft.approval_token,
    )
    return service, send


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["oauth", "ledger"])
@pytest.mark.parametrize("expired_at", [300, 301])
async def test_gmail_expiry_during_predispatch_work_never_posts(
    gmail_settings, location, expired_at
):
    now = [0]
    provider = GoogleMock()
    reservations = []

    def respond(request):
        response = provider(request)
        if location == "oauth" and request.url.path.endswith("userinfo"):
            now[0] = expired_at
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service, send = await prepare(gmail_settings, client, clock=lambda: now[0])
        assert send.preview.expires_at == 300
        ledger = service.transport.gmail.ledger
        original_reserve = ledger.reserve

        async def delayed_reserve(key):
            reservations.append(key)
            claim = await original_reserve(key)
            if location == "ledger":
                now[0] = expired_at
            return claim

        ledger.reserve = delayed_reserve
        now[0] = 299
        result = await service.handle(send)
        assert result.status == "error" and result.delivery == "not_attempted"
        assert result.error_code == "approval_expired"
        assert not provider.sends
        if location == "oauth":
            assert not reservations
        else:
            assert len(reservations) == 1
            # Expired post-reservation approvals retain their durable claim.
            reopened = SqliteLedger(gmail_settings.gmail_ledger_path)
            duplicate = await reopened.reserve(reservations[0])
            assert not duplicate.new and duplicate.status == "reserved"
        request_count = len(provider.requests)
        replay = await service.handle(send)
        assert replay.error_code == "approval_invalid"
        assert len(provider.requests) == request_count and not provider.sends


@pytest.mark.asyncio
async def test_gmail_direct_transport_call_without_verified_expiry_fails_closed(gmail_settings):
    provider = GoogleMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)
        result = await service.transport.send(send.preview)
        assert result.status == "error" and result.delivery == "not_attempted"
        assert result.error_code == "approval_invalid"
        assert not provider.requests


@pytest.mark.asyncio
async def test_gmail_exact_mime_and_consent_precedes_every_external_call(gmail_settings):
    provider = GoogleMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)
        assert not provider.requests
        send.confirmed = False
        assert (await service.handle(send)).error_code == "approval_invalid"
        send.confirmed = True
        original = send.preview
        send.preview = original.model_copy(update={"recipient": "changed@example.com"})
        assert (await service.handle(send)).error_code == "approval_invalid"
        assert not provider.requests
        send.preview = original
        result = await service.handle(send)
        assert result.delivery == "provider_accepted"
        assert len(provider.sends) == 1
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(provider.sends[0]["raw"])
        )
        assert message["From"] == SENDER and message["To"] == str(original.recipient)
        assert message["Subject"] == original.subject
        assert message.get_content() == original.text
        assert message["Cc"] is None and message["Bcc"] is None
        assert message["Message-ID"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"email": "wrong@example.com", "email_verified": True, "sub": "selected-sub"},
        {"email": SENDER, "email_verified": False, "sub": "selected-sub"},
        {"email": SENDER, "email_verified": True, "sub": "different-sub"},
    ],
)
async def test_gmail_identity_failure_is_predispatch_and_creates_no_claim(gmail_settings, identity):
    provider = GoogleMock(identity=identity)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)
        result = await service.handle(send)
        assert result.status == "error" and result.delivery == "not_attempted"
        assert result.error_code == "gmail_authorization_failed"
        assert not provider.sends
        # A later authorized attempt must still own the first reservation.
        provider.identity = {"email": SENDER, "email_verified": True, "sub": "selected-sub"}
        assert (await service.handle(send)).delivery == "provider_accepted"


@pytest.mark.asyncio
async def test_gmail_refresh_failure_is_sanitized_and_not_attempted(gmail_settings, caplog):
    provider = GoogleMock(auth_failure=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)
        result = await service.handle(send)
        assert result.delivery == "not_attempted" and not provider.sends
        assert "SECRET_PROVIDER_ERROR" not in result.model_dump_json() + caplog.text
        assert "TEST_ACCESS" not in caplog.text


@pytest.mark.asyncio
async def test_gmail_parallel_replicas_and_restart_dispatch_at_most_once(gmail_settings):
    provider = GoogleMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        first, send = await prepare(gmail_settings, client)
        second = EmailService(gmail_settings, transport=MailTransport(gmail_settings, client))
        results = await asyncio.gather(first.handle(send), second.handle(send))
        assert any(result.delivery == "provider_accepted" for result in results)
        restarted = EmailService(gmail_settings, transport=MailTransport(gmail_settings, client))
        assert (await restarted.handle(send)).delivery == "provider_accepted"
        assert len(provider.sends) == 1
        raw = open(gmail_settings.gmail_ledger_path, "rb").read()
        assert b"student@example.com" not in raw and b"Exact preview" not in raw
        assert b"TEST_REFRESH" not in raw


@pytest.mark.asyncio
async def test_gmail_timeout_never_reposts_even_after_restart(gmail_settings):
    provider = GoogleMock(timeout=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        first, send = await prepare(gmail_settings, client)
        assert (await first.handle(send)).status == "delivery_unknown"
        second = EmailService(gmail_settings, transport=MailTransport(gmail_settings, client))
        assert (await second.handle(send)).status == "delivery_unknown"
        assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_atomic_sqlite_claim_survives_reopen_and_is_never_released(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledgers = [SqliteLedger(path), SqliteLedger(path)]
    claims = await asyncio.gather(*(ledger.reserve("a" * 64) for ledger in ledgers))
    assert sum(claim.new for claim in claims) == 1
    duplicate = await SqliteLedger(path).reserve("a" * 64)
    assert not duplicate.new and duplicate.status == "reserved"


@pytest.mark.asyncio
async def test_gcs_precondition_duplicate_and_conditional_completion():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"status": "sent", "provider_id": "stored-id", "time": 1}
            )
        return httpx.Response(412)

    async def token():
        return "MOCK_ADC_TOKEN"

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ledger = GcsLedger("example-project-email-agent-ledger", client, token)
        claim = await ledger.reserve("a" * 64)
        assert not claim.new and claim.status == "sent" and claim.provider_id == "stored-id"
        assert requests[0].url.params["ifGenerationMatch"] == "0"
        assert requests[0].method == "POST"


@pytest.mark.asyncio
async def test_gcs_timeout_and_cas_completion_fail_closed():
    async def token():
        return "MOCK_ADC_TOKEN"

    mode = "timeout"

    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("Reservation uncertain")
        if request.url.params["ifGenerationMatch"] == "0":
            return httpx.Response(200, json={"generation": "123"})
        return httpx.Response(412)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ledger = GcsLedger("example-project-email-agent-ledger", client, token)
        with pytest.raises(httpx.ReadTimeout):
            await ledger.reserve("b" * 64)
        mode = "complete"
        claim = await ledger.reserve("b" * 64)
        assert claim.new and claim.generation == "123"
        assert not await ledger.complete(claim, "sent", "known-provider-id")


@pytest.mark.parametrize(
    "change",
    [
        {"account_email": "wrong@example.com"},
        {"scopes": ["openid", "email"]},
        {"project_id": "wrong-project"},
        {"refresh_token": ""},
        {"token_uri": "https://attacker.invalid/token"},
    ],
)
def test_bad_gmail_credential_files_fail_closed(gmail_settings, change):
    from pathlib import Path

    path = Path(gmail_settings.gmail_credentials_file)
    values = json.loads(path.read_text())
    values.update(change)
    path.write_text(json.dumps(values))
    with pytest.raises(OAuthError):
        load_credentials(path)


def test_cloud_run_never_uses_ephemeral_sqlite(gmail_settings, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "email-agent")
    with pytest.raises(ValueError, match="registro GCS compartido"):
        EmailService(gmail_settings)


@pytest.mark.asyncio
async def test_refresh_scope_failure_and_reservation_uncertainty_never_send(gmail_settings):
    provider = GoogleMock(scopes={"openid", "email"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)
        assert (await service.handle(send)).delivery == "not_attempted"
        assert not provider.sends
        provider.scopes = SCOPES

        class UncertainLedger:
            async def reserve(self, key):
                raise httpx.ReadTimeout("Reservation outcome unknown")

        service.transport.gmail.ledger = UncertainLedger()
        assert (await service.handle(send)).error_code == "gmail_ledger_uncertain"
        assert not provider.sends


@pytest.mark.asyncio
async def test_completion_failure_preserves_reserved_claim_and_never_reposts(gmail_settings):
    provider = GoogleMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)

        async def unavailable(*args):
            return False

        service.transport.gmail.ledger.complete = unavailable
        assert (await service.handle(send)).error_code == "gmail_ledger_completion_uncertain"
        restart = EmailService(gmail_settings, transport=MailTransport(gmail_settings, client))
        assert (await restart.handle(send)).status == "delivery_unknown"
        assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_changed_credential_configuration_invalidates_signed_preview(gmail_settings):
    from pathlib import Path

    provider = GoogleMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service, send = await prepare(gmail_settings, client)
        path = Path(gmail_settings.gmail_credentials_file)
        data = json.loads(path.read_text())
        data["refresh_token"] = "ROTATED_REFRESH_TOKEN"
        path.write_text(json.dumps(data))
        restart = EmailService(gmail_settings, transport=MailTransport(gmail_settings, client))
        assert (await restart.handle(send)).error_code == "approval_invalid"
        assert not provider.requests
