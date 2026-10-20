import hashlib
import re
import time
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from common.config import Settings
from common.contracts import (
    EmailChatRequest,
    EmailContent,
    EmailPreview,
    EmailRequest,
    EmailResponse,
)
from common.conversation import ADDRESS, normalized, recipient_from, unquoted
from common.model import EmailDecision, ModelPort
from common.runtime import BoundedGraph, CapacityExceeded
from common.workflows import WorkflowRunner
from email_agent.approval import ApprovalSigner, canonical
from email_agent.transport import MailTransport


class MailState(TypedDict, total=False):
    request: object
    chat_message: str
    response: EmailResponse
    route: str
    semantic: dict
    send_intent: bool
    progress: dict


class EmailService:
    def __init__(self, settings=None, model=None, transport=None):
        self.settings = settings or Settings.from_env("email")
        self.workflow = WorkflowRunner.from_settings(
            self.settings, "email", EmailResponse, clock=lambda: self.clock()
        )
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
        for name in [
            "validate",
            "interpret",
            "draft",
            "send",
        ]:
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "validate")
        graph.add_conditional_edges(
            "validate",
            lambda s: s["route"],
            {
                "finish": END,
                "interpret": "interpret",
                "draft": "draft",
                "send": "send",
            },
        )
        graph.add_conditional_edges(
            "interpret", lambda s: "draft" if s["route"] == "draft" else END
        )
        graph.add_conditional_edges("draft", lambda s: "send" if s.get("send_intent") else END)
        graph.add_edge("send", END)
        self.graph = graph.compile()

    async def handle(self, request):
        if self.workflow:

            async def execute(prepared):
                if (
                    self.settings.model_mode == "vertex"
                    and request.message is not None
                    and request.context is None
                ):
                    previous = await self.workflow.call(
                        "snapshot", prepared.request_id, prepared.session_id
                    )
                    response = (previous or {}).get("response") or {}
                    if response.get("draft") or response.get("preview"):
                        prepared = prepared.model_copy(update={"context": None})
                return await self._handle(prepared)

            return await self.workflow.run(request, execute)
        return await self._handle(request)

    async def _handle(self, request):
        if request.message is not None:
            return await self._chat(
                EmailChatRequest(
                    message=request.message,
                    context=request.context,
                    session_id=request.session_id,
                    request_id=request.request_id,
                    plan_version=request.plan_version,
                    operation_id=request.operation_id,
                ),
                itinerary=request.itinerary,
                recipient=request.recipient,
                content=request.content,
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
        if self.workflow:

            async def execute(prepared):
                if self.settings.model_mode == "vertex" and request.context is None:
                    previous = await self.workflow.call(
                        "snapshot", prepared.request_id, prepared.session_id
                    )
                    response = (previous or {}).get("response") or {}
                    if response.get("draft") or response.get("preview"):
                        prepared = prepared.model_copy(update={"context": None})
                return await self._chat(prepared, itinerary)

            return await self.workflow.run(request, execute)
        return await self._chat(request, itinerary)

    async def _chat(self, request, itinerary=None, recipient=None, content=None):
        explicit = recipient_from(request.message)
        if recipient and explicit and recipient != explicit:
            return EmailResponse(
                status="error",
                delivery="not_attempted",
                error_code="recipient_mismatch",
                message="El destinatario estructurado no coincide con el humano actual.",
            )
        snapshot = (
            await self.workflow.call("snapshot", request.request_id, request.session_id)
            if self.workflow and request.request_id
            else None
        )
        previous = (snapshot or {}).get("response") or {}
        state = {
            "session_id": request.session_id or str(uuid4()),
            "recipient": recipient,
            "request": "",
            "preview": None,
            "approval_token": None,
            "request_id": request.operation_id or request.request_id or str(uuid4()),
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
                    message="El contexto no es válido, no corresponde a esta sesión o venció.",
                    conversation_state="failed",
                )
        preview = state.get("preview") or previous.get("preview")
        durable_recipient = None
        if snapshot:
            inputs = await self.workflow.call(
                "recent_inputs", request.request_id, request.session_id
            )
            # Las direcciones históricas son datos de la conversación, nunca permiso.
            for data in inputs:
                durable_recipient = data.get("recipient") or recipient_from(
                    data.get("message") or ""
                )
                if durable_recipient:
                    break
        current_draft = previous.get("draft")
        if not current_draft and content:
            current_draft = content.model_dump()
        if not current_draft and preview:
            current_draft = {key: preview[key] for key in ("subject", "text")}
        state["draft"] = EmailContent.model_validate(current_draft) if current_draft else None
        state["recipient"] = (
            explicit
            or recipient
            or state["recipient"]
            or (preview["recipient"] if preview else None)
            or durable_recipient
        )
        state["recipient_changed"] = bool(
            preview and recipient and recipient != preview["recipient"] and explicit != recipient
        )
        # Una cortesía o una redacción posterior no borra el recibo de un efecto.
        receipts = [item.get("receipt") or {} for item in (snapshot or {}).get("operations", [])]
        state["receipt"] = next(
            (
                item
                for item in reversed([*receipts, previous])
                if item.get("status") in {"sent", "simulated", "delivery_unknown"}
            ),
            None,
        )
        return await self.converse(
            request.message, semantic={"request": request, "known": state, "itinerary": itinerary}
        )

    async def converse(self, message, *, semantic=None):
        progress = {}
        state = {"chat_message": message, "progress": progress}
        if semantic is not None:
            state["semantic"] = semantic
        try:
            return (await self.runtime.run(self.graph, state))["response"]
        except CapacityExceeded:
            return EmailResponse(
                status="error",
                message="La capacidad de solicitudes está agotada; no se intentó ningún envío.",
                delivery="not_attempted",
                error_code="capacity_exceeded",
                conversation_state="failed",
            )
        except TimeoutError:
            if progress.get("send_started"):
                return self.uncertain_conversation_send()
            return EmailResponse(
                status="error",
                message="La solicitud superó el tiempo máximo disponible. "
                "No se intentó ningún envío.",
                delivery="not_attempted",
                error_code="deadline_or_capacity",
                conversation_state="failed",
            )
        except Exception:
            if progress.get("send_started"):
                return self.uncertain_conversation_send()
            return EmailResponse(
                status="error",
                message="No se pudo obtener una respuesta del modelo. No se intentó ningún envío.",
                delivery="not_attempted",
                error_code="model_error",
                conversation_state="failed",
            )

    @staticmethod
    def uncertain_conversation_send():
        return EmailResponse(
            status="delivery_unknown",
            delivery="unknown",
            error_code="conversation_send_unknown",
            conversation_state="failed",
            message="No se pudo confirmar el resultado del envío. No se reintentó.",
        )

    async def interpret(self, state):
        semantic = state["semantic"]
        request, known, itinerary = semantic["request"], semantic["known"], semantic["itinerary"]
        authority = unquoted(request.message).strip()
        receipt = EmailResponse.model_validate(known["receipt"]) if known["receipt"] else None
        receipt_recipient = str(receipt.preview.recipient) if receipt and receipt.preview else None
        decision = EmailDecision.model_validate(
            await self.model.invoke(
                {
                    "operation": "email_turn",
                    "current_human": request.message,
                    "authority_text": authority,
                    "reference": {
                        "recipient": known["recipient"],
                        "itinerary": itinerary.model_dump(mode="json") if itinerary else None,
                        "available_content": known["request"],
                        "draft": known["draft"].model_dump() if known["draft"] else None,
                        "receipt": {
                            "status": receipt.status,
                            "delivery": receipt.delivery,
                            "recipient": receipt_recipient,
                        }
                        if receipt
                        else None,
                    },
                }
            )
        )
        if decision.intent == "chat":
            message = normalized(decision.message)
            claimed_recipients = [
                recipient_from(candidate) for candidate in ADDRESS.findall(decision.message)
            ]
            if (
                re.search(
                    r"\b(?:llego|entregad[oa]|leid[oa]|inbox|"
                    r"cancele|cancelad[oa]|retire|retirad[oa])\b",
                    message,
                )
                or re.search(
                    r"\b(?:envie|enviamos|enviad[oa]|mande|mandad[oa]|sent)\b",
                    message,
                )
                and not (
                    receipt
                    and receipt.status == "sent"
                    and receipt.delivery == "provider_accepted"
                    and all(
                        recipient is not None and recipient == receipt_recipient
                        for recipient in claimed_recipients
                    )
                )
            ):
                raise ValueError("unverified_chat_claim")
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="completed",
                    message=decision.message,
                    draft=known["draft"],
                    delivery="not_attempted",
                    conversation_state="draft" if known["draft"] else "completed",
                ),
            }
        if decision.intent in {"send", "cancel"} and (
            not authority or decision.evidence != authority
        ):
            raise ValueError("current_human_evidence_required")
        if decision.intent == "send" and known["recipient_changed"]:
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="error",
                    delivery="not_attempted",
                    error_code="recipient_mismatch",
                    message="El destinatario cambió. "
                    "Indique el destinatario en la nueva orden de envío.",
                ),
            }
        if decision.intent == "cancel":
            if known["receipt"]:
                receipt = EmailResponse.model_validate(known["receipt"])
                return {
                    "route": "finish",
                    "response": receipt.model_copy(
                        update={
                            "message": "El envío anterior ya tiene un recibo y no se canceló. "
                            "No se inició otro envío."
                        }
                    ),
                }
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="cancelled",
                    conversation_state="cancelled",
                    delivery="not_attempted",
                    draft=known["draft"],
                    message="Se retiró la solicitud de envío. No se envió un correo.",
                ),
            }
        known["intent"] = decision.intent
        content = known["draft"] if decision.content_source == "draft" else None
        if decision.content_source == "draft" and content is None:
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="clarification",
                    delivery="not_attempted",
                    message="No hay una redacción vigente para reutilizar. "
                    "Indique qué desea redactar.",
                ),
            }
        itinerary = itinerary if decision.content_source == "itinerary" else None
        if decision.content_source == "itinerary" and itinerary is None:
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="clarification",
                    delivery="not_attempted",
                    message="No hay un itinerario disponible. Indique qué desea redactar.",
                ),
            }
        if decision.content_source == "message":
            known["request"] = request.message
        if (
            decision.intent == "send"
            and not known["recipient"]
            or not known["request"]
            and itinerary is None
            and content is None
        ):
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="clarification",
                    delivery="not_attempted",
                    message="¿Cuál es el destinatario explícito del correo?"
                    if decision.intent == "send" and not known["recipient"]
                    else "¿Qué desea comunicar en el correo?",
                    context=self.signer.issue_context(
                        {
                            key: value
                            for key, value in known.items()
                            if key not in {"draft", "recipient_changed"}
                        },
                        self.fingerprint,
                        self.clock(),
                    ),
                    draft=known["draft"],
                    conversation_state="draft",
                ),
            }
        if decision.intent == "send" and self.settings.email_transport == "gmail":
            key = hashlib.sha256(
                canonical(
                    {
                        "purpose": "email_conversation_send",
                        "session": known["session_id"],
                        "request_id": known["request_id"],
                        "config": self.fingerprint,
                    }
                )
            ).hexdigest()
            claim = await self.transport.gmail.ledger.reserve(key)
            if not claim or not claim.new:
                return {"route": "finish", "response": self.uncertain_conversation_send()}
        return {
            "route": "draft",
            "send_intent": decision.intent == "send",
            "request": EmailRequest(
                plan_version=request.plan_version,
                recipient=known["recipient"],
                request=known["request"],
                itinerary=itinerary,
                content=content,
                session_id=known["session_id"],
            ),
        }

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
            return {"route": "interpret"}
        request = state["request"]
        if request.action == "send":
            return {"route": "send"}
        if not request.recipient:
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="clarification", message="¿Cuál es el destinatario explícito del correo?"
                ),
            }
        if not request.request.strip() and request.itinerary is None and request.content is None:
            return {
                "route": "finish",
                "response": EmailResponse(
                    status="clarification", message="¿Qué desea comunicar en el correo?"
                ),
            }
        return {"route": "draft"}

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
        if request.content is not None:
            result = request.content.model_dump()
            text = request.content.text
        elif text:
            result = {"subject": "Tu itinerario en Colombia", "text": text}
        else:
            result = await self.model.invoke(
                {"operation": "draft", "request": request.request, "itinerary_text": text}
            )
        if state.get("semantic") and not state.get("send_intent"):
            content = EmailContent(
                subject="Tu itinerario en Colombia"
                if text and not request.content
                else result["subject"],
                text=text or result["text"],
            )
            return {
                "response": EmailResponse(
                    status="completed",
                    conversation_state="draft",
                    delivery="not_attempted",
                    draft=content,
                    message=f"Asunto: {content.subject}\n\n{content.text}",
                )
            }
        # An itinerary body is rendered from the supplied contract; model prose cannot add facts.
        preview = EmailPreview(
            plan_version=request.plan_version or 1,
            draft_id=str(uuid4()),
            session_id=request.session_id,
            recipient=request.recipient,
            subject=request.content.subject
            if request.content
            else "Tu itinerario en Colombia"
            if text
            else result["subject"],
            text=text or result["text"],
            transport=self.settings.email_transport,
            sender=self.settings.email_sender,
            expires_at=int(self.clock()) + 300,
        )
        result = self.preview_reply(preview, request.request)
        if state.get("send_intent"):
            return {
                "response": result,
                "request": EmailRequest(
                    action="send",
                    confirmed=True,
                    session_id=preview.session_id,
                    preview=preview,
                    approval_token=result.approval_token,
                ),
            }
        return {"response": result}

    def _validated_preview(self, req):
        """Verifica el contenido técnico; no solicita otro turno de aprobación."""
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
            return None
        # Copy only after signature/session/config validation; transport gets signed values.
        return req.preview.model_copy(deep=True)

    async def send(self, state):
        preview = self._validated_preview(state["request"])
        if preview is None:
            return {
                "response": EmailResponse(
                    status="error",
                    delivery="not_attempted",
                    error_code="approval_invalid",
                    message="No se pudo validar el contenido, la firma o su vigencia. "
                    "No se intentó el envío.",
                )
            }
        if state.get("send_intent"):
            state["progress"]["send_started"] = True
        if self.settings.email_transport == "gmail":
            response = await self.transport.send(
                preview, approval_expires_at=preview.expires_at, clock=self.clock
            )
        else:
            response = await self.transport.send(preview)
        if state.get("send_intent"):
            response = response.model_copy(
                update={
                    "preview": preview,
                    "conversation_state": "sent"
                    if response.status == "sent" and response.delivery == "provider_accepted"
                    else "completed"
                    if response.status == "simulated"
                    else "failed",
                }
            )
        return {"response": response}
