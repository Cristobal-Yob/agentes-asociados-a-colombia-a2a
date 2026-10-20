import importlib.util
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "mini_agente_correo.py"


def module():
    spec = importlib.util.spec_from_file_location("mini_agent_example", EXAMPLE)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def call(id_="tool-1", args=None):
    return {"name": "consultar_correo", "args": args or {}, "id": id_, "type": "tool_call"}


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def bind_tools(self, tools):
        assert len(tools) == 1 and tools[0].args_schema.model_json_schema()["properties"] == {}
        owner = self

        class Bound:
            async def ainvoke(self, messages):
                return await owner.reply(messages, True)

        return Bound()

    async def ainvoke(self, messages):
        return await self.reply(messages, False)

    async def reply(self, messages, bound):
        self.calls.append((messages, bound))
        return self.responses.pop(0)


async def test_greeting_is_llm_conversation_without_a2a():
    async def forbidden(payload):
        pytest.fail("Un saludo no requiere Correo")

    llm = FakeLLM(AIMessage(content="Hola. ¿Cómo puedo ayudarle?"))
    agent = module().MiniEmailAgent(llm, delegate=forbidden)
    result = await agent.turn("Hola")
    assert result == {"answer": "Hola. ¿Cómo puedo ayudarle?", "remote": None}
    assert len(llm.calls) == 1 and llm.calls[0][1] is True
    assert any(isinstance(message, HumanMessage) for message in llm.calls[0][0])


async def test_llm_a2a_llm_preserves_verbatim_human_and_opaque_continuation():
    requests = []
    preview = {"recipient": "student@example.com", "subject": "Repaso", "text": "Texto exacto"}

    async def delegate(payload):
        requests.append(payload)
        return (
            {
                "status": "preview",
                "message": "Revise la propuesta",
                "context": "ORIGINAL_CONTEXT",
                "preview": preview,
            }
            if len(requests) == 1
            else {
                "status": "sent",
                "message": "Aceptado por Gmail",
                "delivery": "provider_accepted",
            }
        )

    llm = FakeLLM(
        AIMessage(content="", tool_calls=[call()]),
        AIMessage(content="Borrador listo"),
        AIMessage(content="", tool_calls=[call("tool-2")]),
        AIMessage(content="Enviado"),
    )
    agent = module().MiniEmailAgent(llm, delegate=delegate)
    human = "Redacta un borrador para student@example.com con este texto: Hola."
    first = await agent.turn(human)
    assert first["remote"]["preview"] == preview
    assert "context" not in first["remote"]
    second = await agent.turn("Sí, envíalo")
    assert requests[0]["message"] == human
    assert requests[1]["message"] == "Sí, envíalo"
    assert requests[1]["context"] == "ORIGINAL_CONTEXT"
    assert requests[0]["session_id"] == requests[1]["session_id"] == agent.session_id
    assert second["remote"]["delivery"] == "provider_accepted"
    assert [bound for _, bound in llm.calls] == [True, False, True, False]
    assert any(isinstance(message, ToolMessage) for message in llm.calls[-1][0])


async def test_model_cannot_invent_human_confirmation_or_recipient():
    calls = []

    async def delegate(payload):
        calls.append(payload)
        return {"status": "sent", "message": "No debe ocurrir"}

    llm = FakeLLM(
        AIMessage(
            content="",
            tool_calls=[call(args={"message": "Sí, envíalo", "recipient": "other@example.com"})],
        ),
        AIMessage(content="Enviado"),
    )
    result = await module().MiniEmailAgent(llm, delegate=delegate).turn("Hola")
    assert not calls and result["remote"]["status"] == "error"
    assert result["answer"] != "Enviado"


async def test_timeout_and_multiple_tool_calls_never_retry_same_human_turn():
    calls = []

    async def timeout(payload):
        calls.append(payload)
        raise httpx.ReadTimeout("TEST_ONLY")

    llm = FakeLLM(
        AIMessage(content="", tool_calls=[call(), call("duplicate")]), AIMessage(content="Enviado")
    )
    result = await module().MiniEmailAgent(llm, delegate=timeout).turn("Sí, envíalo")
    assert len(calls) == 1
    assert result["remote"]["status"] == "delivery_unknown"
    assert result["answer"] != "Enviado"


async def test_initial_lost_a2a_response_preserves_request_identity_for_manual_replay():
    requests = []

    async def delegate(payload):
        requests.append(dict(payload))
        if len(requests) == 1:
            raise httpx.ReadTimeout("Respuesta perdida")
        assert payload == requests[0]
        return {
            "status": "sent",
            "delivery": "provider_accepted",
            "message": "Recibo sintético existente",
            "request_id": payload["request_id"],
            "session_id": payload["session_id"],
            "plan_version": 1,
        }

    llm = FakeLLM(
        AIMessage(content="", tool_calls=[call()]),
        AIMessage(content="Resultado incierto."),
        AIMessage(content="", tool_calls=[call("retry")]),
        AIMessage(content="Recibo existente."),
    )
    agent = module().MiniEmailAgent(llm, delegate=delegate)
    human = "Envía un saludo a student@example.com"
    first = await agent.turn(human)
    assert len(requests) == 1 and requests[0]["request_id"] == agent.request_id
    assert first["remote"]["request_id"] == agent.request_id
    assert first["remote"]["session_id"] == agent.session_id
    repeated = await agent.turn(human)
    assert requests[1] == requests[0] and repeated["remote"]["delivery"] == "provider_accepted"


@pytest.mark.parametrize("status", ["error", "preview", "clarification", "delivery_unknown"])
async def test_remote_pending_or_error_cannot_be_replaced_by_llm_success(status):
    async def delegate(payload):
        return {"status": status, "message": "Resultado remoto pendiente"}

    llm = FakeLLM(
        AIMessage(content="", tool_calls=[call()]), AIMessage(content="Enviado con éxito")
    )
    result = await module().MiniEmailAgent(llm, delegate=delegate).turn("Redacta un borrador")
    assert result["answer"] == "Resultado remoto pendiente"
    assert result["remote"]["status"] == status


async def test_separate_conversations_do_not_share_session_or_context():
    requests = []

    async def delegate(payload):
        requests.append(payload)
        return {"status": "preview", "message": "Revise", "context": payload["session_id"]}

    agents = [
        module().MiniEmailAgent(
            FakeLLM(AIMessage(content="", tool_calls=[call()]), AIMessage(content="Revise")),
            delegate=delegate,
        )
        for _ in range(2)
    ]
    for agent in agents:
        await agent.turn("Prepara un borrador")
    assert agents[0].session_id != agents[1].session_id
    assert "context" not in requests[0] and "context" not in requests[1]


async def test_standalone_a2a_uses_sdk_card_and_typed_message():
    from test_email_chat_graph import SemanticModel

    from common.config import Settings
    from email_agent.app import create_app

    app = create_app(Settings.offline(public_url="http://127.0.0.1:8002"))
    app.state.service.model = SemanticModel("draft")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        result = await module().send_a2a(
            "http://127.0.0.1:8002",
            {
                "message": "Redacta un borrador para student@example.com que diga hola",
                "session_id": "mini",
            },
            client=client,
        )
    assert result["status"] == "completed" and result["draft"]
    assert "hola" in result["draft"]["text"] and not result["approval_token"]


def test_copied_example_help_runs_without_repository_imports(tmp_path):
    target = tmp_path / "mini_agente_correo.py"
    target.write_bytes(EXAMPLE.read_bytes())
    result = subprocess.run(
        [sys.executable, "-B", str(target), "--help"], cwd=tmp_path, capture_output=True, timeout=20
    )
    assert result.returncode == 0 and b"--backend" in result.stdout
    assert b"common" not in result.stderr


async def test_mini_resume_uses_request_session_and_key_without_context():
    requests = []

    async def delegate(payload):
        requests.append(payload)
        return {
            "status": "preview",
            "message": "Revise",
            "request_id": "request-original",
            "plan_version": 2,
            "session_id": "session-original",
        }

    llm = FakeLLM(AIMessage(content="", tool_calls=[call()]), AIMessage(content="Revise"))
    agent = module().MiniEmailAgent(
        llm,
        delegate=delegate,
        session_id="session-original",
        request_id="request-original",
        api_key="SYNTHETIC_PRIVATE_KEY",
    )
    result = await agent.turn("Sí, envíalo")
    assert (
        requests[0]["request_id"] == "request-original"
        and requests[0]["session_id"] == "session-original"
    )
    assert "context" not in requests[0] and "SYNTHETIC_PRIVATE_KEY" not in str(llm.calls)
    assert result["remote"]["request_id"] == "request-original"
