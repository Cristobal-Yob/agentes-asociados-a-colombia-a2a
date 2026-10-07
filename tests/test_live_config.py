from dataclasses import replace

import google.auth
import langchain_google_genai
import pytest
from google.auth.exceptions import DefaultCredentialsError
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from common.config import Settings
from common.contracts import TourismRequest, TripContext
from common.model import ModelPort
from email_agent.service import EmailService
from tourism_agent.service import TourismService


def clear_profile(monkeypatch):
    for name in [
        "MODEL_MODE",
        "VERTEX_PROJECT_ID",
        "VERTEX_LOCATION",
        "MODEL_ID",
        "EMAIL_TRANSPORT",
        "EMAIL_SENDER",
        "RESEND_API_KEY",
        "APPROVAL_SIGNING_KEY",
        "ENABLE_REAL_EMAIL",
        "ENABLE_MCP",
        "PUBLIC_AGENT_URL",
        "EMAIL_AGENT_URL",
        "GMAIL_CREDENTIALS_FILE",
        "GMAIL_LEDGER",
        "GMAIL_LEDGER_PATH",
        "GMAIL_LEDGER_BUCKET",
    ]:
        monkeypatch.delenv(name, raising=False)


def fake_vertex(monkeypatch, result=None, error=None):
    captured = {}
    credentials = object()

    def adc(**kwargs):
        captured["adc"] = kwargs
        return credentials, "ambient-project-must-not-be-used"

    def factory(**kwargs):
        captured["model"] = kwargs

        def invoke(prompt):
            if error:
                raise error
            return result or AIMessage(
                content='{"ids": ["bogota-monserrate"]}',
                usage_metadata={"input_tokens": 120, "output_tokens": 20, "total_tokens": 140},
            )

        return RunnableLambda(invoke)

    monkeypatch.setattr(google.auth, "default", adc)
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", factory)
    return captured, credentials


def test_default_profile_is_real_vertex_and_selected_gmail(monkeypatch):
    clear_profile(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "unrelated-ambient-project")
    settings = Settings.from_env("tourism")
    assert settings.model_mode == "vertex"
    assert settings.vertex_project == "example-project"
    assert settings.vertex_location == "global"
    assert settings.model_id == "gemini-3.1-flash-lite"
    assert settings.email_transport == "gmail"
    assert settings.email_sender == "student@example.com"


@pytest.mark.asyncio
async def test_explicit_vertex_adc_and_generation_limits_ignore_ambient_api_keys(monkeypatch):
    clear_profile(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "MUST_NOT_USE_THIS_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "MUST_NOT_USE_THIS_KEY")
    captured, credentials = fake_vertex(monkeypatch)
    port = ModelPort(Settings.from_env("tourism"))
    result = await port.invoke({"operation": "select", "sources": [{"id": "bogota-monserrate"}]})
    assert result == {"ids": ["bogota-monserrate"]}
    assert captured["adc"] == {
        "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
        "quota_project_id": "example-project",
    }
    model = captured["model"]
    assert model["vertexai"] is True and model["credentials"] is credentials
    assert model["project"] == "example-project" and model["location"] == "global"
    assert model["api_key"] is None
    assert model["model"] == "gemini-3.1-flash-lite"
    assert model["temperature"] == 1 and model["thinking_level"] == "minimal"
    assert model["max_output_tokens"] == 700 and model["timeout"] == 12
    assert model["max_retries"] == 0
    assert port.calls == 1
    assert port.last_usage == {"input_tokens": 120, "output_tokens": 20, "total_tokens": 140}


def test_missing_adc_fails_without_fixture_fallback(monkeypatch):
    clear_profile(monkeypatch)

    def missing(**kwargs):
        raise DefaultCredentialsError("Do not expose credential diagnostics")

    monkeypatch.setattr(google.auth, "default", missing)
    with pytest.raises(ValueError, match="No hay ADC de Vertex disponibles"):
        ModelPort(Settings.from_env("tourism"))


def test_unknown_backend_never_silently_uses_fixtures():
    with pytest.raises(ValueError, match="Modo de modelo no admitido"):
        ModelPort(replace(Settings(), model_mode="typo"))


def test_email_missing_gmail_configuration_fails_before_model_or_transport(monkeypatch):
    clear_profile(monkeypatch)
    with pytest.raises(ValueError, match="GMAIL_CREDENTIALS_FILE.*APPROVAL_SIGNING_KEY"):
        EmailService(Settings.from_env("email"))


def test_selected_resend_missing_configuration_is_actionable(monkeypatch):
    clear_profile(monkeypatch)
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    with pytest.raises(ValueError, match="RESEND_API_KEY.*EMAIL_SENDER.*APPROVAL_SIGNING_KEY"):
        EmailService(Settings.from_env("email"))


def test_tourism_does_not_require_email_secrets(monkeypatch):
    clear_profile(monkeypatch)
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    fake_vertex(monkeypatch)
    service = TourismService(Settings.from_env("tourism"))
    assert service.settings.model_mode == "vertex"


@pytest.mark.asyncio
async def test_live_provider_failure_returns_error_and_does_not_use_fixture_model(monkeypatch):
    clear_profile(monkeypatch)
    fake_vertex(monkeypatch, error=RuntimeError("Provider unavailable"))
    service = TourismService(Settings.from_env("tourism"))
    result = await service.handle(
        TourismRequest(context=TripContext(city="Bogota", days=2, interests=["culture"]))
    )
    assert result.status == "error" and result.itinerary is None
    assert service.model.calls == 1


@pytest.mark.asyncio
async def test_live_call_allowance_is_enforced_before_second_dispatch(monkeypatch):
    clear_profile(monkeypatch)
    fake_vertex(monkeypatch)
    port = ModelPort(replace(Settings.from_env("tourism"), model_call_limit=1))
    await port.invoke({"operation": "select", "sources": [{"id": "bogota-monserrate"}]})
    with pytest.raises(ValueError, match="model_call_limit"):
        await port.invoke({"operation": "select", "sources": [{"id": "bogota-monserrate"}]})
    assert port.calls == 1
