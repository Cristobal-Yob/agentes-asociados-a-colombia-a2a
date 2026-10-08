import hashlib
import re
import time
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from common.config import Settings
from common.contracts import EmailChatRequest, EmailPreview, EmailRequest, EmailResponse
from common.conversation import (
    cancels_delivery,
    confirms_preview,
    email_intent,
    has_message_content,
    normalized,
    recipient_from,
    unquoted,
)
from common.model import ModelPort
from common.runtime import BoundedGraph, CapacityExceeded
from email_agent.approval import ApprovalSigner, canonical
from email_agent.transport import MailTransport


class MailState(TypedDict, total=False):
    request: object
    chat_message: str
    response: EmailResponse
    route: str
    authorized_preview: EmailPreview
    authorized_expires_at: int


class EmailService:
    def __init__(self, settings=None, model=None, transport=None):
        self.settings = settings or Settings.from_env("email")
        if self.settings.email_transport == "unconfigured":
            raise ValueError(
                "El proveedor y el remitente de correo real no están configurados. Seleccione un "
                "proveedor y autorice sus credenciales; no se utiliza un reemplazo simulado."
            )
        if self.settings.email_transport in {"resend", "gmail"}:
            missing = []
            if self.settings.email_transport == "resend" and not self.settings.resend_api_key:
                missing.append("RESEND_API_KEY")
            if (
                self.settings.email_transport == "gmail"
                and not self.settings.gmail_credentials_file
            ):
                missing.append("GMAIL_CREDENTIALS_FILE (archivo OAuth de escritorio autorizado)")
            if not self.settings.email_sender:
                missing.append("EMAIL_SENDER")
            if len(self.settings.approval_secret.encode()) < 32:
                missing.append("APPROVAL_SIGNING_KEY (al menos 32 bytes)")
            if not self.settings.real_email_enabled:
                missing.append("ENABLE_REAL_EMAIL=true")
            if missing:
                raise ValueError("La configuración de correo real exige: " + ", ".join(missing))
        elif self.settings.email_transport != "mock":
            raise ValueError("Transporte de correo no admitido")
        self.signer = ApprovalSigner(self.settings.approval_secret)
        self.transport = transport or MailTransport(self.settings)
        self.model = model or ModelPort(self.settings)
        self.clock = time.time
        self.runtime = BoundedGraph()
        self.fingerprint = hashlib.sha256(
            canonical(
                {
                    "transport": self.settings.email_transport,
                    "sender": self.settings.email_sender,
                    "enabled": self.settings.real_email_enabled,
                    "transport_config_digest": getattr(self.transport, "config_digest", ""),
                    "api_key_digest": hashlib.sha256(
                        self.settings.resend_api_key.encode()
                    ).hexdigest(),
                }
            )
        ).hexdigest()
        graph = StateGraph(MailState)
        for name in ["validate", "clarify", "respond_to_chat", "draft", "authorize", "send"]:
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "validate")
        graph.add_conditional_edges(
            "validate",
            lambda s: s["route"],
            {
                "clarify": "clarify",
                "respond_to_chat": "respond_to_chat",
                "draft": "draft",
                "authorize": "authorize",
            },
        )
        graph.add_edge("clarify", END)
        graph.add_edge("respond_to_chat", END)
        graph.add_edge("draft", END)
        graph.add_conditional_edges("authorize", lambda s: "send" if s["route"] == "send" else END)
        graph.add_edge("send", END)
        self.graph = graph.compile()

    async def handle(self, request):
        if request.message is not None:
            return await self.chat(
                EmailChatRequest(
                    message=request.message, context=request.context, session_id=request.session_id
                ),
                itinerary=request.itinerary,
            )
        try:
            return (await self.runtime.run(self.graph, {"request": request}))["response"]
        except CapacityExceeded:
            return EmailResponse(
                status="error",
                message="La capacidad de solicitudes está agotada; no se intentó ningún envío.",
                delivery="not_attempted",
                error_code="capacity_exceeded",
            )
        except TimeoutError:
            if request.action == "send":
                return EmailResponse(
                    status="delivery_unknown",
                    message=(
                        "Se agotó el tiempo máximo de la solicitud de envío; "
                        "no es posible determinar la entrega de forma segura."
                    ),
                    delivery="unknown",
                    error_code="deadline_or_capacity",
                )
            return EmailResponse(
                status="error",
                message="La solicitud superó el tiempo máximo o la capacidad disponible.",
                error_code="deadline_or_capacity",
            )
        except Exception:
            if request.action == "send":
                return EmailResponse(
                    status="delivery_unknown",
                    message="No es posible determinar el resultado del envío de forma segura.",
                    delivery="unknown",
                    error_code="processing_error",
                )
            return EmailResponse(
                status="error",
                message="No se pudo preparar el borrador de forma segura.",
                error_code="processing_error",
            )

    async def chat(self, request, itinerary=None):
        state = {
            "session_id": request.session_id or str(uuid4()),
            "recipient": None,
            "request": "",
            "preview": None,
            "approval_token": None,
            "intent": "none",
            "request_id": str(uuid4()),
        }
        if request.context:
            try:
                state = self.signer.open_context(
                    request.context, self.fingerprint, request.session_id, self.clock()
                )
            except ValueError:
                return EmailResponse(
                    status="error",
                    delivery="not_attempted",
                    error_code="approval_invalid",
                    message="El contexto no es válido, no corresponde a esta sesión o venció. "
                    "Solicite una nueva vista previa; no se intentó el envío.",
                    conversation_state="failed",
                )
        if cancels_delivery(request.message):
            return EmailResponse(
                status="clarification",
                delivery="not_attempted",
                message="No se intentó el envío. Puede iniciar una nueva solicitud de correo.",
                conversation_state="draft",
            )
        if state["preview"]:
            if not confirms_preview(request.message):
                if re.search(
                    r"\b(?:cambia|cambiar|modifica|modificar|corrige|corregir|pero)\b",
                    normalized(unquoted(request.message)),
                ):
                    original = EmailPreview.model_validate(state["preview"])
                    state["preview"], state["approval_token"] = None, None
                    if "@" in request.message:
                        state["recipient"] = recipient_from(request.message)
                        if state["recipient"] and not re.search(
                            r"\b(?:texto|asunto|contenido|mensaje)\b", normalized(request.message)
                        ):
                            preview = original.model_copy(
                                update={
                                    "draft_id": str(uuid4()),
                                    "recipient": state["recipient"],
                                    "expires_at": int(self.clock()) + 300,
                                }
                            )
                            return self.preview_reply(preview, state["request"])
                    state["request"] = ""
                    return EmailResponse(
                        status="clarification",
                        delivery="not_attempted",
                        message="¿Cuál es el destinatario explícito del correo?"
                        if not state["recipient"]
                        else "¿Qué desea comunicar en la nueva vista previa? "
                        "Deberá revisarla y confirmarla antes de enviar.",
                        context=self.signer.issue_context(state, self.fingerprint, self.clock()),
                        conversation_state="draft",
                    )
                return EmailResponse(
                    status="clarification",
                    preview=EmailPreview.model_validate(state["preview"]),
                    message="No se intentó el envío. Para confirmar la vista previa exacta, "
                    "responda «Sí, envíalo». Para modificarla, inicie una nueva solicitud sin "
                    "context; deberá revisar y confirmar la nueva vista previa.",
                    context=request.context,
                    conversation_state="awaiting_confirmation",
                )
            result = await self.handle(
                EmailRequest(
                    action="send",
                    confirmed=True,
                    session_id=state["session_id"],
                    preview=state["preview"],
                    approval_token=state["approval_token"],
                )
            )
            changes = {"conversation_state": "failed"}
            if result.status == "sent" and result.delivery == "provider_accepted":
                changes = {
                    "conversation_state": "sent",
                    "message": "Correo enviado y aceptado por el proveedor. "
                    "La aceptación no garantiza su llegada al buzón.",
                }
            elif result.status == "simulated":
                changes = {"conversation_state": "completed"}
            return result.model_copy(update=changes)
        intent = email_intent(request.message)
        if intent != "none":
            state["intent"] = intent
        if state.get("intent", "none") == "none":
            return await self.converse(request.message)
        if "@" in request.message:
            state["recipient"] = recipient_from(request.message)
        if has_message_content(request.message):
            state["request"] = request.message
        if not state["recipient"] or not state["request"] and itinerary is None:
            context = self.signer.issue_context(state, self.fingerprint, self.clock())
            return EmailResponse(
                status="clarification",
                message="¿Cuál es el destinatario explícito del correo? Indique una sola dirección."
                if not state["recipient"]
                else "¿Qué desea comunicar en el correo?",
                context=context,
                conversation_state="draft",
            )
        if state.get("intent") == "send" and self.settings.email_transport == "gmail":
            key = hashlib.sha256(
                canonical(
                    {
                        "purpose": "email_conversation_send",
                        "session": state["session_id"],
                        "request_id": state["request_id"],
                        "config": self.fingerprint,
                    }
                )
            ).hexdigest()
            try:
                claim = await self.transport.gmail.ledger.reserve(key)
            except Exception:
                claim = None
            if claim is None or not claim.new:
                return EmailResponse(
                    status="delivery_unknown",
                    delivery="unknown",
                    error_code="conversation_already_claimed",
                    conversation_state="failed",
                    message="Esta solicitud de envío ya fue procesada, está en curso o no pudo "
                    "reservarse de forma segura. No se volvió a redactar ni enviar. "
                    "Revise el resultado anterior antes de iniciar otra solicitud.",
                )
        result = await self.handle(
            EmailRequest(
                recipient=state["recipient"],
                request=state["request"],
                itinerary=itinerary,
                session_id=state["session_id"],
            )
        )
        if state.get("intent") == "send" and result.status == "preview":
            return await self.send_composed(result)
        return (
            result
            if result.conversation_state
            else result.model_copy(
                update={"conversation_state": "failed" if result.status == "error" else "draft"}
            )
        )

    async def converse(self, message):
        try:
            return (await self.runtime.run(self.graph, {"chat_message": message}))["response"]
        except CapacityExceeded:
            return EmailResponse(
                status="error",
                message="La capacidad de solicitudes está agotada; no se intentó ningún envío.",
                delivery="not_attempted",
                error_code="capacity_exceeded",
                conversation_state="failed",
            )
        except TimeoutError:
            return EmailResponse(
                status="error",
                message="La solicitud superó el tiempo máximo disponible. "
                "No se intentó ningún envío.",
                delivery="not_attempted",
                error_code="deadline_or_capacity",
                conversation_state="failed",
            )
        except Exception:
            return EmailResponse(
                status="error",
                message="No se pudo obtener una respuesta del modelo. No se intentó ningún envío.",
                delivery="not_attempted",
                error_code="model_error",
                conversation_state="failed",
            )

    async def send_composed(self, draft):
        # La orden humana explícita autoriza redactar y enviar. La firma fija los bytes
        # compuestos antes de pasar al mismo control durable que usa /api/email.
        result = await self.handle(
            EmailRequest(
                action="send",
                confirmed=True,
                session_id=draft.preview.session_id,
                preview=draft.preview,
                approval_token=draft.approval_token,
            )
        )
        return result.model_copy(
            update={
                "preview": draft.preview,
                "conversation_state": "sent"
                if result.status == "sent" and result.delivery == "provider_accepted"
                else "completed"
                if result.status == "simulated"
                else "failed",
            }
        )

    def preview_reply(self, preview, request_text):
        token = self.signer.issue(preview, self.fingerprint)
        context = self.signer.issue_context(
            {
                "session_id": preview.session_id,
                "recipient": str(preview.recipient),
                "request": request_text,
                "preview": preview.model_dump(mode="json"),
                "approval_token": token,
            },
            self.fingerprint,
            self.clock(),
            preview.expires_at,
        )
        return EmailResponse(
            status="preview",
            message="Revise el destinatario, el asunto y el texto. El envío exige la confirmación "
            "explícita de esta vista previa exacta dentro de sus 300 segundos de vigencia. "
            "Si está de acuerdo, responda «Sí, envíalo» con el context devuelto.",
            preview=preview,
            approval_token=token,
            context=context,
            conversation_state="awaiting_confirmation",
        )

    async def validate(self, state):
        if "chat_message" in state:
            return {"route": "respond_to_chat"}
        request = state["request"]
        if request.action == "send":
            return {"route": "authorize"}
        if not request.recipient:
            return {
                "route": "clarify",
                "response": EmailResponse(
                    status="clarification", message="¿Cuál es el destinatario explícito del correo?"
                ),
            }
        if not request.request.strip() and request.itinerary is None:
            return {
                "route": "clarify",
                "response": EmailResponse(
                    status="clarification", message="¿Qué desea comunicar en el correo?"
                ),
            }
        return {"route": "draft"}

    async def clarify(self, state):
        return {}

    async def respond_to_chat(self, state):
        result = await self.model.invoke({"operation": "chat", "request": state["chat_message"]})
        reply = result["message"]
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 4000:
            raise ValueError("invalid_chat_output")
        return {
            "response": EmailResponse(
                status="completed",
                message=reply,
                delivery="not_attempted",
                conversation_state="completed",
            )
        }

    async def draft(self, state):
        request = state["request"]
        text = ""
        if request.itinerary:
            itinerary = request.itinerary
            text = (
                itinerary.summary
                + "\n\n"
                + "\n".join(f"Día {day.day}: {', '.join(day.activities)}" for day in itinerary.days)
            )
            text += "\n\n" + "\n".join(itinerary.warnings)
            text += "\n\nFuentes:\n" + "\n".join(
                f"{s.name}: {s.url} ({s.provenance}; "
                f"fuente revisada {s.source_reviewed_at}; consulta en vivo {s.retrieved_at})"
                for s in itinerary.sources
            )
        result = await self.model.invoke(
            {"operation": "draft", "request": request.request, "itinerary_text": text}
        )
        # An itinerary body is rendered from the supplied contract; model prose cannot add facts.
        preview = EmailPreview(
            draft_id=str(uuid4()),
            session_id=request.session_id,
            recipient=request.recipient,
            subject="Tu itinerario en Colombia" if text else result["subject"],
            text=text or result["text"],
            transport=self.settings.email_transport,
            sender=self.settings.email_sender,
            expires_at=int(self.clock()) + 300,
        )
        return {"response": self.preview_reply(preview, request.request)}

    async def authorize(self, state):
        req = state["request"]
        if (
            not req.confirmed
            or not req.preview
            or not req.approval_token
            or req.recipient is not None
            and str(req.recipient) != str(req.preview.recipient)
            or req.preview.transport != self.settings.email_transport
            or req.preview.sender != self.settings.email_sender
            or not self.signer.verify(
                req.approval_token, req.preview, req.session_id, self.fingerprint, self.clock()
            )
        ):
            return {
                "route": "rejected",
                "response": EmailResponse(
                    status="error",
                    message=(
                        "Se exige confirmación explícita y una vista previa válida, "
                        "sin modificaciones y vigente."
                    ),
                    error_code="approval_invalid",
                ),
            }
        # Copy only after signature/session/config validation; transport gets signed values.
        preview = req.preview.model_copy(deep=True)
        return {
            "route": "send",
            "authorized_preview": preview,
            "authorized_expires_at": preview.expires_at,
        }

    async def send(self, state):
        preview = state["authorized_preview"]
        if self.settings.email_transport == "gmail":
            response = await self.transport.send(
                preview, approval_expires_at=state["authorized_expires_at"], clock=self.clock
            )
        else:
            response = await self.transport.send(preview)
        return {"response": response}
