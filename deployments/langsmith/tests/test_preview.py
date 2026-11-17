import importlib.util
import json
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def graph(name):
    config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))
    path, export = config["graphs"][name].rsplit(":", 1)
    spec = importlib.util.spec_from_file_location("colombia_preview", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, export), module


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("COLOMBIA_LANGSMITH_MODEL", "fixture")
    monkeypatch.setenv("EMAIL_TRANSPORT", "gmail")
    monkeypatch.setenv("ENABLE_REAL_EMAIL", "true")
    monkeypatch.setenv("WORKFLOW_STORE", "firestore")


def test_config_exports_the_two_deployed_agents():
    config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))
    assert set(config["graphs"]) == {"tourism", "email"}
    assert config["source"] == {"kind": "uv"}
    assert "env" not in config


def test_packaged_runtime_matches_deployed_source():
    evidence = json.loads((ROOT / "deployed-source.json").read_text(encoding="utf-8"))
    actual = {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in evidence["runtime"]}
    assert actual == evidence["runtime"]
    assert "tourism_agent/service.py" in actual
    assert "email_agent/service.py" in actual


@pytest.mark.asyncio
async def test_tourism_runs_the_existing_grounded_itinerary_flow():
    agent, _ = graph("tourism")
    result = await agent.ainvoke({"city": "Bogotá", "days": 2, "interests": ["culture"]})
    assert result["model_profile"] == "fixture"
    assert result["status"] == "completed"
    assert result["itinerary"]["city"] == "Bogota"
    assert len(result["itinerary"]["days"]) == 2


@pytest.mark.asyncio
async def test_email_runs_only_drafting_and_does_not_expose_approval(monkeypatch):
    agent, module = graph("email")

    async def unexpected_send(*args, **kwargs):
        pytest.fail("A draft must not enter the transport")

    monkeypatch.setattr(module.NoSendTransport, "send", unexpected_send)
    result = await agent.ainvoke({"message": "Redacta un recordatorio de reunión."})
    assert result["model_profile"] == "fixture"
    assert result["status"] == "completed"
    assert result["draft"]["text"] == "Redacta un recordatorio de reunión."
    assert result["delivery"] == "not_attempted"
    assert set(result) == {"status", "model_profile", "draft", "delivery"}


@pytest.mark.asyncio
async def test_even_a_model_send_decision_cannot_reach_gmail(monkeypatch):
    agent, module = graph("email")
    from common.model import EmailDecision, ModelPort
    from email_agent import gmail_transport

    class SendDecisionModel(ModelPort):
        def __init__(self, settings):
            super().__init__(module.Settings.offline())

        async def invoke(self, payload):
            if payload["operation"] != "email_turn":
                return await super().invoke(payload)
            return EmailDecision(
                intent="send",
                evidence=payload["authority_text"],
                message="Preparing the classroom message.",
                content_source="message",
            )

    def forbidden_provider(*args, **kwargs):
        pytest.fail("The preview constructed a live mail transport")

    monkeypatch.setattr(gmail_transport, "GmailTransport", forbidden_provider)
    monkeypatch.setattr(module, "ModelPort", SendDecisionModel)
    monkeypatch.setenv("COLOMBIA_LANGSMITH_MODEL", "vertex")
    result = await agent.ainvoke({"message": "Envía ahora un saludo a alumno@example.com."})
    assert result["status"] == "error"
    assert result["delivery"] == "not_attempted"
    assert result["error_code"] == "live_delivery_disabled"
    assert "preview" not in result
    assert "approval_token" not in result
