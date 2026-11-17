"""LangSmith-only entrypoints for the verified Colombia agent snapshot."""

from os import getenv
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from common.config import Settings
from common.contracts import EmailChatRequest, EmailResponse, TourismRequest, TripContext
from common.model import ModelPort
from email_agent.service import EmailService
from tourism_agent.service import TourismService


class TourismInput(TypedDict, total=False):
    city: str
    days: int
    interests: list[str]
    message: str


class EmailInput(TypedDict):
    message: str


class Result(TypedDict):
    status: str
    model_profile: Literal["fixture", "vertex"]
    detail: NotRequired[str]
    itinerary: NotRequired[dict]
    draft: NotRequired[dict]
    delivery: NotRequired[Literal["not_attempted"]]
    error_code: NotRequired[str]


class TourismState(TourismInput, Result):
    pass


class EmailState(EmailInput, Result):
    pass


class NoSendTransport:
    """No provider client or ledger exists in this preview transport."""

    async def send(self, *_args, **_kwargs):
        return EmailResponse(
            status="error",
            message="El envío de correo está deshabilitado en esta prueba.",
            delivery="not_attempted",
            error_code="live_delivery_disabled",
        )


class FixtureDraftModel(ModelPort):
    async def invoke(self, payload):
        if payload["operation"] == "email_turn":
            return {
                "intent": "draft",
                "message": "Prueba de redacción con modelo simulado.",
                "evidence": "",
                "content_source": "message",
            }
        return await super().invoke(payload)


def preview_settings(**overrides):
    profile = getenv("COLOMBIA_LANGSMITH_MODEL", "fixture")
    if profile not in {"fixture", "vertex"}:
        raise ValueError("COLOMBIA_LANGSMITH_MODEL must be fixture or vertex")
    # Do not read Cloud Run email, partner, workflow, or authentication settings.
    return Settings.offline(model_mode=profile, **overrides)


async def plan_trip(state: TourismInput) -> Result:
    context = TripContext.model_validate(
        {key: state[key] for key in ("city", "days", "interests") if key in state}
    )
    message = state.get("message", "")
    with TemporaryDirectory(prefix="colombia-langsmith-") as path:
        settings = preview_settings(workflow_db_path=str(Path(path) / "workflow.sqlite"))
        if settings.model_mode == "fixture" and message:
            return {
                "status": "error",
                "model_profile": "fixture",
                "error_code": "fixture_requires_structured_trip",
                "detail": "En el modo de prueba, use los campos city, days e interests.",
            }
        # No A2A peer or remote catalog is configured, including in Vertex mode.
        reply = await TourismService(settings=settings).handle(
            TourismRequest(message=message, context=context)
        )
    result = {
        "status": reply.status,
        "model_profile": settings.model_mode,
        "detail": reply.message,
    }
    if reply.itinerary:
        result["itinerary"] = reply.itinerary.model_dump(mode="json")
    if reply.error_code:
        result["error_code"] = reply.error_code
    return result


async def draft_email(state: EmailInput) -> Result:
    settings = preview_settings()
    model = FixtureDraftModel(settings) if settings.model_mode == "fixture" else ModelPort(settings)
    service = EmailService(settings=settings, model=model, transport=NoSendTransport())
    reply = await service.chat(EmailChatRequest(message=state["message"]))
    result = {
        "status": reply.status,
        "model_profile": settings.model_mode,
        "delivery": "not_attempted",
    }
    if reply.draft:
        result["draft"] = reply.draft.model_dump(mode="json")
    if reply.error_code:
        result["error_code"] = reply.error_code
    return result


def compile_preview(state, inputs, name, node):
    graph = StateGraph(state, input_schema=inputs, output_schema=Result)
    graph.add_node(name, node, input_schema=inputs)
    graph.add_edge(START, name)
    graph.add_edge(name, END)
    return graph.compile()


tourism = compile_preview(TourismState, TourismInput, "plan_trip", plan_trip)
email = compile_preview(EmailState, EmailInput, "draft_email", draft_email)
