import asyncio
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from agenda_agent.contracts import AgendaRequest, AgendaSpec, SessionSpec
from common.config import Settings
from common.contracts import (
    Day,
    EmailRequest,
    EmailResponse,
    Itinerary,
    TourismResponse,
    TripContext,
)
from common.model import ModelPort
from common.runtime import BoundedGraph
from common.workflows import WorkflowRunner, child_operation, validation_evidence
from tourism_agent.catalog import Catalog

ACTIVITY_LABELS = {
    "bogota-gold": "Museo del Oro",
    "bogota-candelaria": "Recorrido por La Candelaria",
    "bogota-monserrate": "Mirador de Monserrate",
    "medellin-botero": "Plaza Botero",
    "medellin-arvi": "Recorrido de naturaleza por el Parque Arví",
    "medellin-comuna": "Recorrido guiado por la Comuna 13",
    "cartagena-walls": "Recorrido por la ciudad amurallada",
    "cartagena-clock": "Recorrido por la Torre del Reloj y el Portal de los Dulces",
    "cartagena-castle": "Castillo de San Felipe",
}


class TripState(TypedDict, total=False):
    request: object
    context: TripContext
    response: TourismResponse
    sources: list
    retrieval: str
    missing: str
    error: str
    email_action: str
    confirmation: bool
    agenda_confirmation: bool
    progress: dict


class TourismService:
    def __init__(
        self,
        settings=None,
        peer=None,
        catalog=None,
        model=None,
        agenda_peer=None,
        conversation_model=None,
    ):
        self.settings = settings or Settings.from_env("tourism")
        self.workflow = WorkflowRunner.from_settings(
            self.settings, "tourism", TourismResponse, clock=lambda: self.clock()
        )
        self.clock = time.time
        self.peer = peer
        self.agenda_peer = agenda_peer
        self.catalog = catalog or Catalog()
        self.model = model or ModelPort(self.settings)
        self.conversation = None
        if conversation_model is not None or self.settings.model_mode == "vertex":
            from tourism_agent.conversation import NativeConversation

            native_model = conversation_model or (
                self.model if isinstance(self.model, ModelPort) else ModelPort(self.settings)
            )
            self.conversation = NativeConversation(self, native_model)
        self.runtime = BoundedGraph()
        graph = StateGraph(TripState)
        for name in [
            "understand",
            "clarify",
            "retrieve",
            "plan",
            "delegate",
            "confirm_email",
            "delegate_agenda",
            "confirm_agenda",
            "error",
        ]:
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "understand")
        graph.add_conditional_edges(
            "understand",
            lambda s: (
                "confirm_agenda"
                if s.get("agenda_confirmation")
                else "confirm_email"
                if s.get("confirmation")
                else "clarify"
                if s.get("missing")
                else "retrieve"
            ),
        )
        graph.add_edge("clarify", END)
        graph.add_conditional_edges("retrieve", lambda s: "error" if s.get("error") else "plan")
        graph.add_conditional_edges(
            "plan",
            lambda s: (
                "error"
                if s.get("error")
                else "delegate_agenda"
                if s["context"].agenda_requested
                else "delegate"
                if s["email_action"] == "draft"
                else END
            ),
        )
        graph.add_edge("delegate", END)
        graph.add_edge("confirm_email", END)
        graph.add_edge("delegate_agenda", END)
        graph.add_edge("confirm_agenda", END)
        graph.add_edge("error", END)
        self.graph = graph.compile()

    async def handle(self, request):
        if request.message.strip():
            if self.conversation is None:
                return TourismResponse(
                    status="error",
                    context=request.context,
                    message="El perfil fixture requiere un modelo conversacional inyectado.",
                    error_code="conversation_model_required",
                )
            return await self.conversation.handle(request)
        if self.workflow:
            return await self.workflow.run(request, self._handle)
        return await self._handle(request)

    async def _handle(self, request):
        progress = {}
        try:
            return (await self.runtime.run(self.graph, {"request": request, "progress": progress}))[
                "response"
            ]
        except TimeoutError:
            if progress.get("direct_send"):
                return self.unknown_delegated_send(progress["response"])
            return TourismResponse(
                status="error",
                message="La solicitud superó el tiempo máximo o la capacidad disponible.",
                context=request.context,
                error_code="deadline_or_capacity",
                conversation_state="failed",
            )
        except Exception:
            if progress.get("direct_send"):
                return self.unknown_delegated_send(progress["response"])
            return TourismResponse(
                status="error",
                message="No se pudo procesar esta solicitud de forma segura.",
                context=request.context,
                error_code="processing_error",
                conversation_state="failed",
            )

    async def understand(self, state):
        req = state["request"]
        data = req.context.model_dump()
        data.update(
            draft_email=req.context.draft_email or req.draft_email,
            recipient=req.recipient or req.context.recipient,
        )
        context = TripContext.model_validate(data)
        missing = next(
            (field for field in ["city", "days", "interests"] if not getattr(context, field)), ""
        )
        if not missing and context.draft_email and not context.recipient:
            missing = "recipient"
        return {"context": context, "missing": missing}

    async def clarify(self, state):
        missing = state["missing"]
        question = {
            "city": (
                "¿Qué ciudad desea visitar? Cobertura sin conexión: Bogotá, Medellín y Cartagena."
            ),
            "days": "¿Cuántos días durará la visita (de 1 a 7)?",
            "interests": "¿Qué le interesa más: cultura, naturaleza o gastronomía?",
            "recipient": "¿Cuál es el destinatario explícito del correo?",
        }[missing]
        return {
            "response": TourismResponse(
                status="clarification",
                message=question,
                context=state["context"],
                missing_field=missing,
                conversation_state="draft",
            )
        }

    async def retrieve(self, state):
        sources, status = await self.catalog.retrieve(
            state["context"].city, state["context"].interests
        )
        return {
            "sources": sources,
            "retrieval": status,
            "error": "unsupported_city" if not sources else "",
        }

    async def plan(self, state):
        phase = "select_invocation"
        try:
            result = await self.model.invoke(
                {
                    "operation": "select",
                    "request": state["request"].message,
                    "email_intent": {
                        "requested": state["context"].draft_email,
                        "recipient_supplied": state["context"].recipient is not None,
                    },
                    "interests": state["context"].interests,
                    "sources": [{"id": s.id, "name": s.name} for s in state["sources"]],
                }
            )
            phase = "select_shape"
            if not isinstance(result, dict):
                raise ValueError("invalid_select_shape")
            ids = result["ids"]
            email_action = result["email_action"]
            if email_action not in {"none", "draft"}:
                raise ValueError("invalid_email_action")
            if not state["context"].draft_email or not state["context"].recipient:
                email_action = "none"
            records = {s.id: s for s in state["sources"]}
            phase = "grounded_ids"
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(id_, str) or id_ not in records for id_ in ids)
                or len(ids) != len(set(ids))
            ):
                raise ValueError("ungrounded_model_output")
            selected = [records[id_] for id_ in ids]
            phase = "itinerary_construction"
            days = [
                Day(
                    day=n + 1,
                    activities=[
                        ACTIVITY_LABELS.get(source.id, source.name)
                        if source.provenance == "curated_fixture"
                        else source.name
                        for source in [selected[n % len(selected)]]
                    ],
                    source_ids=[selected[n % len(selected)].id],
                )
                for n in range(state["context"].days)
            ]
            itinerary = Itinerary(
                city=selected[0].city,
                days=days,
                summary=(
                    f"Visita sugerida de {len(days)} días a {selected[0].city}, "
                    "basada en un catálogo reducido para la clase."
                ),
                sources=selected,
                warnings=[
                    "Los precios, horarios, disponibilidad, tiempos de transporte y condiciones "
                    "de seguridad no están verificados. "
                    "Consulte fuentes oficiales antes de viajar.",
                    "Los datos de ejemplo tienen una fecha fija de revisión de la fuente; "
                    "solo los registros de MCP en vivo tienen una fecha de consulta.",
                    "Las actividades pueden repetirse en visitas más largas; "
                    "esta es una sugerencia, no una reserva.",
                    "El catálogo reducido contiene actividades de cultura y naturaleza; "
                    "el interés por la gastronomía no implica recomendaciones verificadas.",
                ],
                model_mode=self.settings.model_mode,
            )
            return {
                "email_action": email_action,
                "response": TourismResponse(
                    status="partial"
                    if state["context"].draft_email and email_action == "none"
                    else "completed",
                    message="Se conservó el itinerario; la planificación no solicitó el borrador "
                    "de correo. Puede volver a pedir su preparación."
                    if state["context"].draft_email and email_action == "none"
                    else "Se preparó un itinerario basado en las fuentes disponibles.",
                    error_code="email_draft_not_selected"
                    if state["context"].draft_email and email_action == "none"
                    else None,
                    context=state["context"],
                    itinerary=itinerary,
                    retrieval=state["retrieval"],
                    conversation_state="failed"
                    if state["context"].draft_email and email_action == "none"
                    else "completed",
                ),
            }
        except Exception as error:
            name = type(error).__name__
            allowed_exceptions = {
                "KeyError",
                "ValueError",
                "TypeError",
                "TimeoutError",
                "ValidationError",
                "OutputParserException",
            }
            reason = {
                "KeyError": "select_missing_field",
                "TypeError": "select_invalid_type",
                "TimeoutError": "model_timeout",
                "OutputParserException": "model_json_invalid",
                "ValidationError": "planner_validation_error",
            }.get(name, "planner_unclassified_error")
            allowed_reasons = {
                "model_call_limit",
                "model_input_limit",
                "model_output_limit",
                "invalid_select_shape",
                "invalid_email_action",
                "ungrounded_model_output",
            }
            if (
                isinstance(error, ValueError)
                and error.args
                and isinstance(error.args[0], str)
                and error.args[0] in allowed_reasons
            ):
                reason = error.args[0]
            diagnostic = {
                "phase": phase,
                "error_code": reason,
                "exception_type": name if name in allowed_exceptions else "OtherError",
            }
            if isinstance(error, ValidationError):
                diagnostic["validation_errors"] = validation_evidence(
                    error.errors(),
                    {
                        "city",
                        "days",
                        "summary",
                        "sources",
                        "warnings",
                        "model_mode",
                        "day",
                        "activities",
                        "source_ids",
                        "id",
                        "name",
                        "url",
                        "provenance",
                        "retrieved_at",
                        "fixture_as_of",
                        "source_reviewed_at",
                    },
                )
            return {"error": "model_or_grounding_error", "diagnostic": diagnostic}

    @staticmethod
    def unknown_delegated_send(response):
        message = (
            "Se conservó el itinerario, pero no se pudo confirmar el resultado del envío. "
            "La entrega es incierta; no se reintentó automáticamente. "
            "Revise el buzón del remitente antes de solicitar otro envío."
        )
        return response.model_copy(
            update={
                "status": "partial",
                "message": message,
                "context": response.context.model_copy(
                    update={"draft_email": False, "email_context": None}
                ),
                "email": EmailResponse(
                    status="delivery_unknown",
                    delivery="unknown",
                    error_code="email_peer_outcome_unknown",
                    message=message,
                    conversation_state="failed",
                ),
                "error_code": "email_peer_outcome_unknown",
                "conversation_state": "failed",
            }
        )

    async def delegate(self, state):
        response = state["response"]
        req = state["request"]
        direct_send = bool(req.message.strip())
        may_send = direct_send or state.get("native", False)
        if direct_send:
            # El contexto no vuelve a conservar una intención cuyo remoto puede haber enviado.
            response = response.model_copy(
                update={
                    "context": response.context.model_copy(
                        update={"draft_email": False, "email_context": None}
                    )
                }
            )
        try:
            if self.peer is None:
                raise RuntimeError("peer_unavailable")
            if may_send:
                # Estado por solicitud, también accesible si vence el límite exterior del grafo.
                state["progress"].update({"direct_send": True, "response": response})
            # El par conversacional decide si hay envío: cualquier llamada nativa puede
            # contener un efecto y comparte los 15 s del SDK, sin reintentos.
            # El cliente HTTP puede vencer a los 12 s y el grafo completo a los 35 s:
            # cualquier corte después de delegar exige resultado incierto, nunca un reintento.
            async with asyncio.timeout(15 if may_send else 8):
                draft = await self.peer(
                    EmailRequest(
                        message=req.message if direct_send or state.get("native") else None,
                        request_id=req.request_id,
                        plan_version=req.plan_version,
                        operation_id=child_operation(req.operation_id, "email"),
                        session_id=req.session_id,
                        recipient=state["context"].recipient,
                        itinerary=response.itinerary,
                    )
                )
            if (
                draft.status in {"sent", "simulated", "delivery_unknown"}
                or may_send
                and draft.status == "error"
            ):
                context = response.context.model_copy(
                    update={"draft_email": False, "email_context": None}
                )
                return {
                    "response": response.model_copy(
                        update={
                            "email": draft,
                            "context": context,
                            "message": draft.message,
                            "status": "completed"
                            if draft.status in {"sent", "simulated"}
                            else "partial",
                            "conversation_state": draft.conversation_state,
                            "error_code": draft.error_code,
                        }
                    )
                }
            if state.get("native") and draft.status in {"completed", "clarification", "cancelled"}:
                return {
                    "response": response.model_copy(
                        update={
                            "email": draft,
                            "message": draft.message,
                            "status": "completed" if draft.status == "cancelled" else draft.status,
                            "context": response.context.model_copy(
                                update={"draft_email": False, "email_context": draft.context}
                            ),
                            "conversation_state": draft.conversation_state,
                        }
                    )
                }
            if draft.status != "preview":
                raise RuntimeError("peer_draft_error")
            context = response.context.model_copy(
                update={"draft_email": False, "email_context": draft.context}
            )
            return {
                "response": response.model_copy(
                    update={
                        "email": draft,
                        "context": context,
                        "conversation_state": "awaiting_confirmation",
                    }
                )
            }
        except Exception:
            if may_send:
                return {"response": self.unknown_delegated_send(response)}
            return {
                "response": response.model_copy(
                    update={
                        "status": "partial",
                        "message": "Se conservó el itinerario; el agente de correo no está "
                        "disponible o no pudo preparar el borrador.",
                        "error_code": "email_peer_unavailable",
                        "conversation_state": "failed",
                    }
                )
            }

    async def confirm_email(self, state):
        req = state["request"]
        context = state["context"]
        # Only email can validate the signed continuation and authorize exact content.
        try:
            if self.peer is None:
                raise RuntimeError("peer_unavailable")
            async with asyncio.timeout(8):
                result = await self.peer(
                    EmailRequest(
                        message=req.message,
                        context=context.email_context,
                        recipient=context.recipient,
                        request_id=req.request_id,
                        plan_version=req.plan_version,
                        operation_id=child_operation(req.operation_id, "email"),
                        session_id=req.session_id,
                    )
                )
            next_context = context.model_copy(update={"email_context": result.context})
            successful = result.status in {"sent", "simulated"}
            return {
                "response": TourismResponse(
                    status="completed"
                    if successful
                    else "clarification"
                    if result.status in {"clarification", "preview"}
                    else "error",
                    message=result.message,
                    context=next_context,
                    email=result,
                    error_code=result.error_code,
                    conversation_state=result.conversation_state,
                )
            }

        except Exception:
            attempted = True
            return {
                "response": TourismResponse(
                    status="error",
                    context=context,
                    error_code="email_peer_unavailable",
                    message="No se obtuvo una respuesta del agente de correo. "
                    "No se puede confirmar el resultado del envío; no se reintentó automáticamente."
                    if attempted
                    else "El agente de correo no está disponible; no se confirmó un envío.",
                    email=EmailResponse(
                        status="delivery_unknown" if attempted else "error",
                        delivery="unknown" if attempted else "not_attempted",
                        message="No se obtuvo el resultado del agente de correo.",
                        conversation_state="failed",
                    ),
                    conversation_state="failed",
                )
            }

    async def delegate_agenda(self, state):
        itinerary = state["response"].itinerary
        spec = AgendaSpec(
            sessions=[
                SessionSpec(title=", ".join(day.activities), day_offset=day.day - 1)
                for day in itinerary.days
            ]
        )
        return await self.call_agenda(state, spec)

    async def confirm_agenda(self, state):
        return await self.call_agenda(state, None)

    async def call_agenda(self, state, spec):
        req = state["request"]
        base = state.get("response") or TourismResponse(
            status="completed", message="Continuación de agenda.", context=state["context"]
        )
        try:
            if self.agenda_peer is None:
                raise ValueError("agenda_peer_unavailable")
            async with asyncio.timeout(15):
                result = await self.agenda_peer(
                    AgendaRequest(
                        message=req.message,
                        request_id=req.request_id,
                        plan_version=req.plan_version,
                        operation_id=child_operation(req.operation_id, "agenda"),
                        session_id=req.session_id,
                        context=state["context"].agenda_context,
                        spec=spec,
                        recipient=state["context"].recipient,
                    )
                )
            context = base.context.model_copy(
                update={
                    "agenda_context": result.context
                    if result.status != "created" or (result.email or {}).get("status") == "preview"
                    else None,
                    "agenda_requested": result.status != "created",
                    "draft_email": False,
                }
            )
            return {
                "response": base.model_copy(
                    update={
                        "agenda": result,
                        "context": context,
                        "message": result.message,
                        "status": "clarification"
                        if result.status == "clarification"
                        else "partial"
                        if result.status in {"error", "conflict", "creation_unknown"}
                        else "completed",
                        "missing_field": result.missing_field,
                        "error_code": result.error_code,
                        "conversation_state": "awaiting_confirmation"
                        if result.status == "preview"
                        else "failed"
                        if result.status in {"error", "creation_unknown"}
                        else "completed",
                        "email": EmailResponse.model_validate(result.email)
                        if result.email
                        else None,
                    }
                )
            }
        except Exception:
            return {
                "response": base.model_copy(
                    update={
                        "status": "partial",
                        "error_code": "agenda_peer_unavailable",
                        "message": (
                            "Se conservó el itinerario, pero no se obtuvo el resultado de "
                            "Agenda. Si estaba confirmando, la creación puede ser incierta; no"
                            " se reintentó ni se envió una confirmación por correo."
                        ),
                        "conversation_state": "failed",
                    }
                )
            }

    async def error(self, state):
        return {
            "response": TourismResponse(
                status="error",
                message=(
                    "No hay un itinerario basado en fuentes disponible para esta solicitud. "
                    "Cobertura sin conexión: Bogotá, Medellín y Cartagena."
                ),
                context=state["context"],
                error_code=state["error"],
                retrieval=state.get("retrieval", "not_requested"),
                conversation_state="failed",
            )
        }
