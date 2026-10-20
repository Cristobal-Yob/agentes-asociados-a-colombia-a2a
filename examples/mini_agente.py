"""Miniagente LangGraph para una conversación con Correo o Itinerario por HTTP."""

import json
import os
from typing import Annotated
from uuid import uuid4

import google.auth
import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode

IDS = ("request_id", "session_id", "plan_version")
FIELDS = ("status", "message", "preview", "delivery", "error_code")
PROMPT = "Consulte al especialista en cada turno. No invente confirmaciones ni resultados."
ERROR = "Error remoto; revise URL y clave. No se reintentó."


def create_agent(model, url, key, *, transport=None):
    ids = {"request_id": str(uuid4()), "session_id": str(uuid4())}

    @tool
    def consultar_agente(state: Annotated[dict, InjectedState]) -> str:
        """Consulta al remoto con el último mensaje humano literal, sin argumentos libres."""
        human = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        try:
            with httpx.Client(transport=transport, timeout=15) as client:
                payload = {"message": human, **ids}
                endpoint = url.rstrip("/") + "/api/chat"
                response = client.post(endpoint, headers={"X-API-Key": key}, json=payload)
                response.raise_for_status()
                data = response.json()
            ids.update({k: data[k] for k in IDS if data.get(k) is not None})
            reply = data.get("email") or data
            result = {k: reply[k] for k in FIELDS if k in reply}
            if data.get("itinerary"):
                result["itinerary"] = data["itinerary"]
            return json.dumps({**result, **ids}, ensure_ascii=False, indent=2)
        except Exception:
            return json.dumps({"status": "unknown", "message": ERROR, **ids}, ensure_ascii=False)

    bound = model.bind_tools([consultar_agente], tool_choice="consultar_agente")

    def decide(state):
        answer = bound.invoke([SystemMessage(content=PROMPT), *state["messages"]])
        answer.tool_calls = answer.tool_calls[:1]
        return {"messages": [answer]}

    graph = StateGraph(MessagesState)
    graph.add_node("llm", decide)
    graph.add_node("herramienta", ToolNode([consultar_agente], handle_tool_errors=ERROR))
    graph.add_edge(START, "llm")
    graph.add_edge("llm", "herramienta")
    graph.add_edge("herramienta", END)
    return graph.compile()


def main():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        quota_project_id=os.environ["VERTEX_PROJECT_ID"],
    )
    model = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite",
        vertexai=True,
        project=os.environ["VERTEX_PROJECT_ID"],
        location="global",
        credentials=credentials,
        api_key=None,
        max_retries=0,
        timeout=15,
    )
    agent = create_agent(
        model, os.getenv("AGENT_URL", "http://127.0.0.1:8002"), os.environ["AGENT_API_KEY"]
    )
    messages = []
    while (human := input("Mensaje (salir para terminar): ")).strip().lower() != "salir":
        messages = agent.invoke({"messages": [*messages, HumanMessage(content=human)]})["messages"]
        print(messages[-1].text)


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        pass
    except Exception as error:
        raise SystemExit(
            f"Error del miniagente ({type(error).__name__}); revise configuración."
        ) from None
