import pytest
from langchain_core.messages import AIMessage

from common.model import ModelPort
from tourism_agent import service


@pytest.mark.asyncio
async def test_adversarial_agenda_tool_cannot_use_ambient_configuration(monkeypatch):
    for key, value in {
        "COLOMBIA_LANGSMITH_MODEL": "vertex",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "WORKFLOW_STORE": "firestore",
        "ENABLE_MCP": "true",
        "PEER_USE_IAM": "true",
        "AGENDA_AGENT_URL": "https://agenda.example.invalid",
        "EMAIL_AGENT_URL": "https://email.example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    checked = []

    class SimulatedToolModel(ModelPort):
        def __init__(self, settings):
            assert settings.workflow_store == "sqlite"
            assert not settings.peer_iam
            assert not settings.mcp_enabled
            assert settings.agenda_peer_url == "http://127.0.0.1:8003"
            super().__init__(service.Settings.offline())

        async def tourism_turn(self, messages, tools):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "trip",
                        "name": "generar_itinerario",
                        "args": {"city": "Bogotá", "days": 1, "interests": ["culture"]},
                    },
                    {"id": "agenda", "name": "consultar_agenda", "args": {}},
                ],
            )

        async def tourism_text(self, messages, tools):
            assert "agenda_peer_unavailable" in messages[0].content
            checked.append(True)
            return AIMessage(content="La agenda no está disponible.")

    monkeypatch.setattr(service, "ModelPort", SimulatedToolModel)
    from colombia_preview import tourism

    result = await tourism.ainvoke({"message": "Planifica un día cultural en Bogotá y agéndalo."})
    assert checked and result["itinerary"]["days"]
    assert result["status"] == "partial"
    assert "agenda" not in result
