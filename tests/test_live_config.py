import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import google.auth
import langchain_google_genai
import pytest
from google.auth.exceptions import DefaultCredentialsError
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_google_genai import ChatGoogleGenerativeAI

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
    monkeypatch.setenv("MODEL_MODE", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "example-project")
    monkeypatch.setenv("EMAIL_TRANSPORT", "gmail")


def fake_vertex(monkeypatch, result=None, error=None):
    captured = {}
    credentials = object()

    def adc(**kwargs):
        captured["adc"] = kwargs
        return credentials, "ambient-project-must-not-be-used"

    def factory(**kwargs):
        captured["model"] = kwargs

        def invoke(prompt):
            captured["invocations"] = captured.get("invocations", 0) + 1
            if error:
                raise error
            return result or AIMessage(
                content='{"ids": ["bogota-monserrate"], "email_action": "none"}',
                usage_metadata={"input_tokens": 120, "output_tokens": 20, "total_tokens": 140},
            )

        model = RunnableLambda(invoke)

        def structured(schema, **options):
            captured["structured"] = {"schema": schema, **options}

            def selected(prompt):
                raw = invoke(prompt)
                captured["select_prompt"] = prompt.messages[0].content
                return {"raw": raw, "parsed": json.loads(raw.content), "parsing_error": None}

            return RunnableLambda(selected)

        def native(tools, **options):
            captured["native"] = {"tools": tools, **options}
            return RunnableLambda(invoke)

        model.with_structured_output = structured
        model.bind_tools = native
        return model

    monkeypatch.setattr(google.auth, "default", adc)
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", factory)
    return captured, credentials


def test_default_profile_is_offline_without_borrowed_cloud_configuration(monkeypatch):
    clear_profile(monkeypatch)
    for name in ("MODEL_MODE", "VERTEX_PROJECT_ID", "EMAIL_TRANSPORT", "ENABLE_REAL_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env("tourism")
    assert settings.model_mode == "fixture"
    assert not settings.vertex_project
    assert settings.email_transport == "mock"
    assert not settings.real_email_enabled


@pytest.mark.asyncio
async def test_explicit_vertex_adc_and_generation_limits_ignore_ambient_api_keys(monkeypatch):
    clear_profile(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "MUST_NOT_USE_THIS_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "MUST_NOT_USE_THIS_KEY")
    captured, credentials = fake_vertex(monkeypatch)
    port = ModelPort(Settings.from_env("tourism"))
    result = await port.invoke({"operation": "select", "sources": [{"id": "bogota-monserrate"}]})
    assert result == {"ids": ["bogota-monserrate"], "email_action": "none"}
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


@pytest.mark.parametrize(
    "output",
    [
        {"ids": ["bogota-monserrate"]},
        {"email_action": "none"},
        {"ids": [], "email_action": "none"},
        {"ids": ["unknown-source"], "email_action": "none"},
        {"ids": ["bogota-monserrate"], "email_action": "send"},
        {"ids": ["bogota-monserrate"], "email_action": "none", "confirmed": True},
    ],
)
async def test_select_missing_or_unauthorized_fields_fail_without_defaults_or_retry(
    monkeypatch, output
):
    clear_profile(monkeypatch)
    captured, _ = fake_vertex(monkeypatch, result=AIMessage(content=json.dumps(output)))
    port = ModelPort(Settings.from_env("tourism"))
    with pytest.raises(ValueError, match="invalid_select_shape"):
        await port.invoke({"operation": "select", "sources": [{"id": "bogota-monserrate"}]})
    assert captured["invocations"] == port.calls == 1


async def test_select_native_schema_requires_supplied_ids_and_non_executing_email_action(
    monkeypatch,
):
    clear_profile(monkeypatch)
    captured, _ = fake_vertex(monkeypatch)
    port = ModelPort(Settings.from_env("tourism"))
    expected = {"ids": ["bogota-monserrate"], "email_action": "none"}
    assert (
        await port.invoke(
            {"operation": "select", "sources": [{"id": "bogota-monserrate"}, {"id": "bogota-gold"}]}
        )
        == expected
    )
    binding = captured["structured"]
    assert binding["method"] == "json_schema" and binding["include_raw"] is True
    schema = binding["schema"]
    assert set(schema["required"]) == set(schema["properties"]) == {"ids", "email_action"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["ids"]["minItems"] == 1
    assert schema["properties"]["ids"]["items"] == {
        "type": "string",
        "minLength": 1,
        "enum": ["bogota-monserrate", "bogota-gold"],
    }
    assert schema["properties"]["email_action"]["enum"] == ["none", "draft"]
    assert "Para draft" not in captured["select_prompt"]
    assert port.last_usage == {"input_tokens": 120, "output_tokens": 20, "total_tokens": 140}

    sdk_binding = {}

    def sdk_bind(**options):
        sdk_binding.update(options)
        return RunnableLambda(lambda _: AIMessage(content=json.dumps(expected)))

    runnable = ChatGoogleGenerativeAI.with_structured_output(
        SimpleNamespace(bind=sdk_bind), schema, method="json_schema", include_raw=True
    )
    converted = await runnable.ainvoke([HumanMessage(content="Selección local de prueba")])
    assert converted["parsed"] == expected and converted["parsing_error"] is None
    assert sdk_binding["response_mime_type"] == "application/json"
    assert sdk_binding["response_json_schema"] == schema and "tools" not in sdk_binding


@pytest.mark.parametrize(
    "payload, output",
    [
        ({"operation": "chat"}, {"message": "Hola"}),
        ({"operation": "draft"}, {"subject": "Asunto", "text": "Contenido"}),
        ({"operation": "agenda"}, {"intent": "chat", "message": "Hola", "spec": {}}),
    ],
)
async def test_select_structured_binding_does_not_change_other_operations(
    monkeypatch, payload, output
):
    clear_profile(monkeypatch)
    captured, _ = fake_vertex(monkeypatch, result=AIMessage(content=json.dumps(output)))
    port = ModelPort(Settings.from_env("tourism"))
    assert await port.invoke(payload) == output
    assert "structured" not in captured and captured["invocations"] == 1


async def test_select_binding_preserves_native_auto_and_cancellation(monkeypatch):
    clear_profile(monkeypatch)
    captured, _ = fake_vertex(monkeypatch, result=AIMessage(content="Hola"))
    port = ModelPort(Settings.from_env("tourism"))
    response = await port.tourism_turn([HumanMessage(content="Hola")], [])
    assert response.content == "Hola" and captured["native"]["tool_choice"] is None
    assert "structured" not in captured
    captured, _ = fake_vertex(monkeypatch, error=asyncio.CancelledError())
    port = ModelPort(Settings.from_env("tourism"))
    with pytest.raises(asyncio.CancelledError):
        await port.invoke({"operation": "select", "sources": [{"id": "bogota-monserrate"}]})
    assert captured["invocations"] == port.calls == 1
    assert port.last_usage is None


async def test_email_turn_native_schema_is_required_and_cannot_carry_model_permissions(monkeypatch):
    clear_profile(monkeypatch)
    human = "super mandamelo por correo pls a student@example.com"
    expected = {
        "intent": "send",
        "message": "Prepararé el contenido disponible.",
        "evidence": human,
        "content_source": "message",
    }
    captured, _ = fake_vertex(
        monkeypatch,
        result=AIMessage(
            content=json.dumps(expected),
            usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        ),
    )
    port = ModelPort(Settings.from_env("tourism"))
    payload = {
        "operation": "email_turn",
        "current_human": human,
        "authority_text": human,
        "reference": {
            "recipient": "student@example.com",
            "itinerary": None,
            "available_content": "Contenido aportado por el usuario",
        },
    }
    assert await port.invoke(payload) == expected
    binding = captured["structured"]
    assert binding["method"] == "json_schema" and binding["include_raw"] is True
    schema = binding["schema"]
    assert set(schema["required"]) == {"intent", "message", "evidence"}
    assert set(schema["properties"]) == {"intent", "message", "evidence", "content_source"}
    assert schema["properties"]["content_source"]["enum"] == ["message", "itinerary", "draft"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["intent"]["enum"] == ["chat", "draft", "send", "cancel"]
    assert "email_turn" in captured["select_prompt"]
    assert port.last_usage == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
    assert port.calls == captured["invocations"] == 1

    sdk_binding = {}

    def sdk_bind(**options):
        sdk_binding.update(options)
        return RunnableLambda(lambda _: AIMessage(content=json.dumps(expected)))

    runnable = ChatGoogleGenerativeAI.with_structured_output(
        SimpleNamespace(bind=sdk_bind), schema, method="json_schema", include_raw=True
    )
    converted = await runnable.ainvoke([HumanMessage(content=human)])
    assert converted["parsed"] == expected and converted["parsing_error"] is None
    assert sdk_binding["response_json_schema"] == schema
    assert "tools" not in sdk_binding


@pytest.mark.parametrize(
    "patch",
    [{"confirmed": True}, {"evidence": None}, {"intent": "approve"}, {"content_source": "history"}],
)
async def test_email_turn_malformed_output_fails_once_without_defaults(monkeypatch, patch):
    clear_profile(monkeypatch)
    output = {"intent": "chat", "message": "Hola", "evidence": "", **patch}
    captured, _ = fake_vertex(monkeypatch, result=AIMessage(content=json.dumps(output)))
    port = ModelPort(Settings.from_env("tourism"))
    with pytest.raises(ValueError):
        await port.invoke(
            {
                "operation": "email_turn",
                "current_human": "Hola",
                "authority_text": "Hola",
                "reference": {},
            }
        )
    assert port.calls == captured["invocations"] == 1


async def test_native_text_recovery_disables_tools_in_installed_sdk_and_rejects_calls(monkeypatch):
    from langchain_google_genai._function_utils import _tool_choice_to_tool_config

    clear_profile(monkeypatch)
    captured, _ = fake_vertex(monkeypatch, result=AIMessage(content="Con gusto."))
    port = ModelPort(Settings.from_env("tourism"))
    tools = [{"name": "consultar_correo", "parameters": {"type": "object", "properties": {}}}]
    reply = await port.tourism_text([HumanMessage(content="Gracias")], tools)
    assert reply.content == "Con gusto." and captured["native"]["tool_choice"] == "none"
    config = _tool_choice_to_tool_config(captured["native"]["tool_choice"], ["consultar_correo"])
    assert config.function_calling_config.mode == "NONE"
    captured, _ = fake_vertex(
        monkeypatch,
        result=AIMessage(
            content="", tool_calls=[{"name": "consultar_correo", "args": {}, "id": "forbidden"}]
        ),
    )
    port = ModelPort(Settings.from_env("tourism"))
    with pytest.raises(ValueError, match="unexpected_tool_call"):
        await port.tourism_text([HumanMessage(content="Gracias")], tools)
    assert captured["invocations"] == port.calls == 1


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
    with pytest.raises(ValueError, match="GMAIL_EXPECTED_EMAIL|GMAIL_CREDENTIALS_FILE"):
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
