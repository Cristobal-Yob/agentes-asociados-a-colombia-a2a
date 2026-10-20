import json

import google.auth
import httpx
import langchain_google_genai
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from common.config import Settings
from common.contracts import EmailRequest, TourismRequest
from common.model import ModelPort
from email_agent.app import create_app as email_app
from email_agent.service import EmailService
from tourism_agent.app import create_app as tourism_app
from tourism_agent.catalog import CATALOG


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch):
    async def forbidden_async(*args, **kwargs):
        raise AssertionError("No se permite transporte HTTP externo en esta prueba.")

    def forbidden_sync(*args, **kwargs):
        raise AssertionError("No se permite transporte HTTP externo en esta prueba.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync)


@pytest.mark.parametrize(
    "payload,reply",
    [
        (
            {"operation": "select", "sources": [{"id": "known-id", "name": "Original name"}]},
            {"ids": ["known-id"], "email_action": "none"},
        ),
        (
            {"operation": "draft", "request": "Write a welcome", "itinerary_text": ""},
            {"subject": "Bienvenida", "text": "Le damos la bienvenida."},
        ),
    ],
)
async def test_provider_receives_spanish_instruction_without_changing_payload(
    monkeypatch, payload, reply
):
    from types import SimpleNamespace

    captured = {}
    credentials = object()
    structured = langchain_google_genai.ChatGoogleGenerativeAI.with_structured_output

    def adc(**kwargs):
        captured["adc"] = kwargs
        return credentials, "ignored-ambient-project"

    def provider(**kwargs):
        captured["settings"] = kwargs

        def invoke(prompt):
            captured["messages"] = prompt.messages
            return AIMessage(content=json.dumps(reply))

        model = RunnableLambda(invoke)
        model.with_structured_output = lambda schema, **options: structured(
            SimpleNamespace(bind=lambda **_: model), schema, **options
        )
        return model

    monkeypatch.setattr(google.auth, "default", adc)
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", provider)
    port = ModelPort(
        Settings(model_mode="vertex", vertex_project="example-project", model_call_limit=1)
    )
    assert await port.invoke(payload) == reply
    system, human = captured["messages"]
    assert "español" in system.content.lower()
    assert "identificadores" in system.content.lower()
    if payload["operation"] == "select":
        assert "email_action" in system.content and "Para draft" not in system.content
    else:
        assert "asunto" in system.content.lower() and "texto" in system.content.lower()
        assert "citas" in system.content.lower()
    assert json.loads(human.content) == payload
    assert captured["adc"]["quota_project_id"] == "example-project"
    model = captured["settings"]
    assert model["credentials"] is credentials and model["api_key"] is None
    assert (model["project"], model["location"], model["model"]) == (
        "example-project",
        "global",
        "gemini-3.1-flash-lite",
    )
    assert (model["max_output_tokens"], model["timeout"], model["max_retries"]) == (700, 12, 0)
    assert model["temperature"] == 1 and model["thinking_level"] == "minimal"
    with pytest.raises(ValueError, match="model_call_limit"):
        await port.invoke(payload)
    assert port.calls == 1


async def test_fixture_subject_is_spanish_and_draft_never_dispatches():
    class ForbiddenMail:
        async def send(self, *args, **kwargs):
            raise AssertionError("Un borrador no autoriza el envío.")

    service = EmailService(Settings.offline(), transport=ForbiddenMail())
    body = 'Texto exacto con cita externa: "Original quotation".'
    draft = await service.handle(EmailRequest(recipient="student@example.com", request=body))
    assert draft.status == "preview" and draft.preview.subject == "Borrador de mensaje"
    assert draft.preview.text == body
    assert service.signer.verify(
        draft.approval_token, draft.preview, "classroom", service.fingerprint, service.clock()
    )


@pytest.mark.parametrize(
    "factory,contract,path,payload",
    [
        (tourism_app, TourismRequest, "/api/chat", {"context": {"days": 8}}),
        (tourism_app, TourismRequest, "/api/chat", {"unexpected": True}),
        (email_app, EmailRequest, "/api/email", {"confirmed": "true"}),
        (email_app, EmailRequest, "/api/email", {"recipient": "invalid"}),
    ],
)
async def test_rest_validation_translates_only_message(factory, contract, path, payload):
    try:
        contract.model_validate(payload)
    except ValidationError as error:
        original = error.errors(include_url=False)
    app = factory(Settings.offline())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        response = await client.post(path, json=payload)
    assert response.status_code == 422
    localized = response.json()["detail"]
    assert len(localized) == len(original)
    for actual, expected in zip(localized, original, strict=True):
        assert actual["msg"].startswith("El ") or actual["msg"].startswith("No ")
        assert actual["msg"] != expected["msg"]
        expected["loc"] = ["body", *expected["loc"]]
        assert {key: value for key, value in actual.items() if key != "msg"} == {
            key: value for key, value in expected.items() if key != "msg"
        }


async def test_curated_activity_presentation_preserves_source_records():
    catalog_before = json.dumps(CATALOG, ensure_ascii=False, sort_keys=True)
    app = tourism_app(Settings.offline())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        response = await client.post(
            "/api/chat",
            json={"context": {"city": "Bogota", "days": 2, "interests": ["culture"]}},
        )
    assert response.status_code == 200
    itinerary = response.json()["itinerary"]
    assert itinerary["days"][0]["activities"] == ["Museo del Oro"]
    assert itinerary["days"][1]["activities"] == ["Recorrido por La Candelaria"]
    sources = {item["id"]: item for item in itinerary["sources"]}
    assert sources["bogota-gold"]["name"] == "Gold Museum"
    assert sources["bogota-gold"]["url"] == CATALOG["bogota"][1]
    for day in itinerary["days"]:
        assert all(source_id in sources for source_id in day["source_ids"])
    assert json.dumps(CATALOG, ensure_ascii=False, sort_keys=True) == catalog_before
