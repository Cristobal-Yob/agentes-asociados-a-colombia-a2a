import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class Model:
    def __init__(self, calls=1):
        self.calls = calls
        self.inputs = []

    def bind_tools(self, tools, *, tool_choice=None):
        assert tools[0].name == "consultar_agente"
        assert tool_choice == "consultar_agente"
        return self

    def invoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "consultar_agente",
                    "args": {"confirmed": True, "recipient": "inventado@example.com"},
                    "id": str(i),
                }
                for i in range(max(1, self.calls))
            ],
        )


def conversation(model, url, key, *, transport):
    from examples.mini_agente import create_agent

    class Conversation:
        def __init__(self):
            self.model, self.graph, self.messages = (
                model,
                create_agent(model, url, key, transport=transport),
                [],
            )

        async def turn(self, human):
            state = await self.graph.ainvoke(
                {"messages": [*self.messages, HumanMessage(content=human)]}
            )
            self.messages = state["messages"]
            last = self.messages[-1]
            return (
                json.loads(last.content)
                if isinstance(last, ToolMessage)
                else {"status": "conversation", "message": last.text}
            )

    return Conversation()


@pytest.mark.parametrize("port", [8001, 8002])
async def test_same_adapter_sends_human_literal_and_only_returned_ids(port):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "preview",
                "message": "Revise el borrador",
                "request_id": "request",
                "session_id": "session",
                "plan_version": 2,
                "context": "NO_TRANSPORTAR",
                "approval_token": "NO_MOSTRAR",
                "preview": {"recipient": "real@example.com", "text": "Texto exacto"},
            },
        )

    agent = conversation(
        Model(calls=0 if port == 8001 else 2),
        f"http://127.0.0.1:{port}",
        "clave-privada",
        transport=httpx.MockTransport(handler),
    )
    original = (
        "Prepara únicamente un itinerario: Bogotá, 3 días, cultura."
        if port == 8001
        else "  Redacta un borrador para real@example.com.\nNo lo envíes todavía.  "
    )
    result = await agent.turn(original)
    assert len(requests) == 1 and requests[0].method == "POST"
    assert str(requests[0].url) == f"http://127.0.0.1:{port}/api/chat"
    assert requests[0].headers["X-API-Key"] == "clave-privada"
    first_payload = json.loads(requests[0].content)
    assert set(first_payload) == {"message", "request_id", "session_id"}
    assert first_payload["message"] == original
    assert first_payload["request_id"] and first_payload["session_id"]
    assert result["status"] == "preview" and result["preview"]["text"] == "Texto exacto"
    assert "NO_TRANSPORTAR" not in json.dumps(result) and "NO_MOSTRAR" not in json.dumps(result)
    await agent.turn("Sí, envíalo")
    assert len(requests) == 2
    assert json.loads(requests[-1].content) == {
        "message": "Sí, envíalo",
        "request_id": "request",
        "session_id": "session",
        "plan_version": 2,
    }
    assert "clave-privada" not in str(agent.model.inputs)


async def test_greeting_delegates_to_specialist_with_google_forced_function_config():
    from langchain_google_genai._function_utils import _tool_choice_to_tool_config

    config = _tool_choice_to_tool_config("consultar_agente", ["consultar_agente"])
    assert config.function_calling_config.mode.value == "ANY"
    assert config.function_calling_config.allowed_function_names == ["consultar_agente"]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "message": "Hola del especialista"})

    agent = conversation(
        Model(calls=0), "http://127.0.0.1:8002", "key", transport=httpx.MockTransport(handler)
    )
    result = await agent.turn("Hola")
    assert result["status"] == "completed" and result["message"] == "Hola del especialista"
    assert len(calls) == 1
    assert json.loads(calls[0].content) == {
        "message": "Hola",
        "request_id": result["request_id"],
        "session_id": result["session_id"],
    }
    assert set(agent.graph.get_graph().nodes) >= {"llm", "herramienta"}


@pytest.mark.parametrize("failure", ["timeout", "unauthorized"])
async def test_http_failure_is_visible_and_never_retried(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("No mostrar detalles privados", request=request)
        return httpx.Response(401, text="No mostrar cuerpo privado")

    agent = conversation(
        Model(calls=2), "http://127.0.0.1:8002", "key", transport=httpx.MockTransport(handler)
    )
    result = await agent.turn("Prepara un borrador")
    assert len(calls) == 1 and result["status"] in {"error", "unknown"}
    assert "No mostrar" not in json.dumps(result)
    assert "reintent" in result["message"]


async def test_changing_destination_uses_new_key_and_no_foreign_ids():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "message": "Respuesta",
                "request_id": "mail-id",
                "session_id": "s",
                "plan_version": 1,
            },
        )

    transport = httpx.MockTransport(handler)
    await conversation(Model(), "http://127.0.0.1:8002", "mail-key", transport=transport).turn(
        "Consulta correo"
    )
    await conversation(Model(), "http://127.0.0.1:8001", "trip-key", transport=transport).turn(
        "Bogotá, tres días"
    )
    first, second = (json.loads(request.content) for request in requests)
    assert second["message"] == "Bogotá, tres días"
    assert set(second) == {"message", "request_id", "session_id"}
    assert second["request_id"] != first["request_id"] and second["request_id"] != "mail-id"
    assert second["session_id"] != first["session_id"] and second["session_id"] != "s"
    assert requests[1].headers["X-API-Key"] == "trip-key"


async def test_first_lost_response_keeps_stable_ids_and_exact_manual_retry_payload():
    requests, effects = [], []

    def handler(request):
        payload = json.loads(request.content)
        assert payload["request_id"] and payload["session_id"]
        requests.append(request)
        if len(requests) == 1:
            effects.append(payload)
            raise httpx.ReadTimeout("Respuesta perdida después del efecto", request=request)
        if len(requests) == 2:
            assert request.content == requests[0].content
        return httpx.Response(
            200,
            json={
                "status": "sent" if len(requests) == 2 else "completed",
                "delivery": "provider_accepted" if len(requests) == 2 else "not_attempted",
                "message": "Recibo existente; no se repitió el efecto",
                "request_id": payload["request_id"],
                "session_id": payload["session_id"],
                "plan_version": 2,
                "operation_id": "receipt-operation",
            },
        )

    agent = conversation(
        Model(), "http://127.0.0.1:8002", "key", transport=httpx.MockTransport(handler)
    )
    human = "Envía el correo a student@example.com"
    unknown = await agent.turn(human)
    assert len(requests) == 1 and unknown["status"] == "unknown"
    assert unknown["request_id"] == effects[0]["request_id"]
    assert unknown["session_id"] == effects[0]["session_id"]
    receipt = await agent.turn(human)
    assert receipt["status"] == "sent" and len(effects) == 1
    await agent.turn("¿Qué resultado tuvo el envío anterior?")
    followup = json.loads(requests[-1].content)
    assert followup == {
        "message": "¿Qué resultado tuvo el envío anterior?",
        "request_id": unknown["request_id"],
        "session_id": unknown["session_id"],
        "plan_version": 2,
    }
    assert "operation_id" not in followup and len(effects) == 1
