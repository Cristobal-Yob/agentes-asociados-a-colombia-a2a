import asyncio

import pytest
from langgraph.config import get_config

from common.config import Settings
from common.contracts import EmailChatRequest
from email_agent.service import EmailService


class ChatModel:
    def __init__(self):
        self.calls = []
        self.nodes = []

    async def invoke(self, payload):
        self.calls.append(payload)
        self.nodes.append(get_config()["metadata"]["langgraph_node"])
        return {"message": "Puedo ayudarle a preparar un correo."}


async def test_general_chat_runs_inside_the_mail_graph_without_dispatch():
    model = ChatModel()
    service = EmailService(Settings.offline(), model=model)
    result = await service.chat(EmailChatRequest(message="Hola, ¿qué puedes hacer?"))
    assert result.status == "completed"
    assert result.delivery == "not_attempted"
    assert model.nodes == ["respond_to_chat"]
    assert model.calls == [{"operation": "chat", "request": "Hola, ¿qué puedes hacer?"}]


async def test_general_chat_obeys_the_shared_mail_capacity_without_calling_model():
    model = ChatModel()
    service = EmailService(Settings.offline(), model=model)
    for _ in range(8):
        await service.runtime.slots.acquire()
    try:
        result = await service.chat(EmailChatRequest(message="Hola"))
    finally:
        for _ in range(8):
            service.runtime.slots.release()
    assert result.status == "error"
    assert result.error_code == "capacity_exceeded"
    assert result.delivery == "not_attempted"
    assert model.calls == []


async def test_general_chat_cancellation_releases_the_shared_slot():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingModel:
        async def invoke(self, payload):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    service = EmailService(Settings.offline(), model=WaitingModel())
    task = asyncio.create_task(service.chat(EmailChatRequest(message="Hola")))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert service.runtime.slots._value == 7
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()
    assert service.runtime.slots._value == 8


async def test_general_chat_deadline_is_reported_without_dispatch():
    model = ChatModel()
    service = EmailService(Settings.offline(), model=model)

    async def expired(graph, state):
        raise TimeoutError

    service.runtime.run = expired
    result = await service.chat(EmailChatRequest(message="Hola"))
    assert result.status == "error"
    assert result.error_code == "deadline_or_capacity"
    assert result.delivery == "not_attempted"
    assert model.calls == []
