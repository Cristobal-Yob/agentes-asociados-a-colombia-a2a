"""Decisiones programadas de pruebas; nunca interpretan vocabulario humano."""

from copy import deepcopy

from langchain_core.messages import AIMessage


def tool(name, args=None, identifier="tool-1"):
    return {"name": name, "args": args or {}, "id": identifier, "type": "tool_call"}


class ScriptedConversation:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    async def tourism_turn(self, messages, tools):
        self.messages.append(deepcopy(messages))
        assert self.responses, "Falta una decisión explícita en la prueba."
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def tourism_text(self, messages, tools):
        return await self.tourism_turn(messages, tools)


def conversation(*names, city="Bogota", days=2):
    calls = [
        tool(
            name,
            {"city": city, "days": days, "interests": ["culture"]}
            if name == "generar_itinerario"
            else {},
            f"tool-{number}",
        )
        for number, name in enumerate(names)
    ]
    return ScriptedConversation(
        AIMessage(content="", tool_calls=calls), AIMessage(content="Resultado recibido.")
    )
