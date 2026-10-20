import asyncio
import hashlib
import re
import time
from datetime import UTC, datetime
from uuid import uuid4

from agenda_agent.calendar import GoogleCalendar
from agenda_agent.contracts import (
    AgendaExtraction,
    AgendaPreview,
    AgendaResponse,
    AgendaSpec,
    CreatedEvent,
)
from agenda_agent.ledger import AgendaLedger
from agenda_agent.planning import missing_field, overlaps, plan_events
from agenda_agent.signing import AgendaSigner, canonical
from common.config import Settings
from common.contracts import EmailRequest
from common.conversation import (
    agenda_intent,
    normalized,
    recipient_from,
    unquoted,
)
from common.model import ModelPort
from common.workflows import WorkflowRunner, child_operation

QUESTIONS = {
    "date": "¿Qué fecha inicial desea usar (AAAA-MM-DD)?",
    "timezone": "¿Qué zona horaria IANA corresponde, por ejemplo America/Bogota?",
    "start_time": "¿A qué hora local deben comenzar las actividades (HH:MM)?",
    "sessions": "¿Qué actividades desea organizar?",
    "duration_minutes": "¿Cuántos minutos dura cada actividad?",
}


def confirms_agenda(message):
    text = " ".join(re.sub(r"[,!.;¿?¡]+", " ", normalized(unquoted(message))).split())
    return text in {
        "si agendalo",
        "si agenda",
        "confirmo agenda",
        "confirmo crear",
        "si crealos",
        "crear eventos",
    }


class AgendaService:
    """Caso de uso independiente; no importa FastAPI ni ejecuta efectos sin aprobación."""

    def __init__(
        self, settings=None, *, model=None, calendar=None, ledger=None, email_peer=None, clock=None
    ):
        self.settings = settings or Settings.from_env("agenda")
        self.workflow = WorkflowRunner.from_settings(
            self.settings, "agenda", AgendaResponse, clock=lambda: self.clock()
        )
        self.model = model or ModelPort(self.settings)
        self.calendar = calendar or (
            GoogleCalendar(self.settings.calendar_credentials_file)
            if self.settings.calendar_credentials_file
            else None
        )
        self.ledger = ledger or AgendaLedger(self.settings.calendar_ledger_path)
        self.email_peer = email_peer
        self.clock = clock or time.time
        fingerprint = hashlib.sha256(
            canonical(
                {
                    "calendar": self.calendar.fingerprint if self.calendar else "unconfigured",
                    "ledger": str(self.ledger.path),
                    "email_peer": self.settings.peer_url,
                }
            )
        ).hexdigest()
        self.signer = AgendaSigner(self.settings.agenda_approval_secret, fingerprint)
        self.slots = asyncio.Semaphore(8)

    def reply(self, request, state, **values):
        return AgendaResponse(
            context=self.signer.issue(request.session_id, state, self.clock()), **values
        )

    async def handle(self, request):
        if self.workflow:

            async def resume(prepared):
                saved = await self.workflow.call(
                    "snapshot", prepared.request_id, prepared.session_id
                )
                previous = (saved or {}).get("response") or {}
                if (
                    not prepared.context
                    and previous.get("error_code") == "calendar_not_configured"
                    and previous.get("pending_plan")
                ):
                    # Recupera datos validados, sin crear aprobación ni confirmar eventos.
                    prepared = prepared.model_copy(
                        update={
                            "context": self.signer.issue(
                                prepared.session_id, previous["pending_plan"], self.clock()
                            )
                        }
                    )
                if not prepared.context and previous.get("events"):
                    try:
                        accepted_state = self.signer.open(
                            previous.get("context"), prepared.session_id, self.clock()
                        )
                    except ValueError:
                        accepted_state = {}
                    history = [
                        previous,
                        *[item.get("receipt") or {} for item in reversed(saved["operations"])],
                    ]
                    latest = next(
                        (
                            item.get("preview") or (item.get("email") or {}).get("preview")
                            for item in history
                            if item.get("preview") or (item.get("email") or {}).get("preview")
                        ),
                        {},
                    )
                    prepared = prepared.model_copy(
                        update={
                            "context": self.signer.issue(
                                prepared.session_id,
                                {
                                    "accepted_events": previous["events"],
                                    "recipient": recipient_from(prepared.message)
                                    or accepted_state.get("recipient")
                                    or latest.get("recipient"),
                                    "summary_content": accepted_state.get("summary_content")
                                    or (latest if "text" in latest else None),
                                },
                                self.clock(),
                            )
                        }
                    )
                return await self._handle(prepared)

            return await self.workflow.run(
                request, resume, confirmed_action=confirms_agenda(request.message)
            )
        return await self._handle(request)

    async def _handle(self, request):
        try:
            async with asyncio.timeout(35):
                async with self.slots:
                    return await self.run(request)
        except TimeoutError:
            return AgendaResponse(
                status="creation_unknown" if confirms_agenda(request.message) else "error",
                message=(
                    "Se agotó el plazo. No se reintenta una creación incierta; use la "
                    "misma continuación para consultar su resultado."
                ),
                error_code="deadline_exceeded",
            )
        except Exception:
            return AgendaResponse(
                status="error",
                message=(
                    "No se pudo procesar la agenda de forma verificable. No se simuló "
                    "una operación de Calendar."
                ),
                error_code="agenda_unavailable",
            )

    async def run(self, request):
        try:
            state = (
                self.signer.open(request.context, request.session_id, self.clock())
                if request.context
                else {}
            )
        except ValueError:
            return AgendaResponse(
                status="error",
                message=(
                    "La aprobación no es válida, venció o pertenece a otra sesión/configuración."
                ),
                error_code="approval_invalid",
            )
        if normalized(unquoted(request.message)) in {"cancelar", "cancela", "no", "salir"}:
            return AgendaResponse(
                status="completed", message="La solicitud se canceló; no se crearon eventos."
            )
        if state.get("accepted_events"):
            return await self.summary_followup(request, state)
        if state.get("preview"):
            preview = AgendaPreview.model_validate(state["preview"])
            if confirms_agenda(request.message):
                return await self.confirm(preview, request.context)
            return AgendaResponse(
                status="preview",
                message=(
                    "Revise la propuesta. Responda «Sí, agéndalo» para crear "
                    "exactamente estos eventos o «cancelar» para detenerse. Para "
                    "cambiarla, solicite una nueva propuesta."
                ),
                preview=preview,
                context=request.context,
            )
        previous = AgendaSpec.model_validate(state.get("spec", {}))
        extraction = AgendaExtraction.model_validate(
            await self.model.invoke(
                {
                    "operation": "agenda",
                    "request": request.message,
                    "previous": previous.model_dump(mode="json"),
                }
            )
        )
        requested_plan = agenda_intent(request.message) or re.search(
            r"\b(?:organiza(?:me)?|programa(?:me)?|planifica(?:me)?)\b",
            normalized(unquoted(request.message)),
        )
        if (
            extraction.intent == "chat"
            and not requested_plan
            and request.spec is None
            and not state.get("spec")
        ):
            return AgendaResponse(
                status="completed",
                message=extraction.message
                or (
                    "Puedo organizar actividades y consultar Calendar. Crear eventos "
                    "exige revisar una propuesta y confirmarla."
                ),
            )
        values = previous.model_dump(mode="json")
        for key, value in extraction.spec.model_dump(mode="json").items():
            if value:
                values[key] = value
        if request.spec:
            values.update(
                {key: value for key, value in request.spec.model_dump(mode="json").items() if value}
            )
        spec = AgendaSpec.model_validate(values)
        # Fechas/horas/duraciones no pueden ser inventadas por el modelo. Los campos
        # tipados son otra entrada explícita; las aclaraciones conservan lo ya validado.
        for key in ("date", "timezone", "start_time"):
            value = getattr(spec, key)
            explicit = getattr(request.spec, key) if request.spec else None
            if (
                value
                and value != explicit
                and value != getattr(previous, key)
                and value not in request.message
            ):
                setattr(spec, key, None)
        durations = re.findall(r"\b([1-9]\d{0,2})\s*(?:minutos|min)\b", normalized(request.message))
        old_durations = {item.duration_minutes for item in previous.sessions}
        for index, item in enumerate(spec.sessions):
            explicit = (
                request.spec.sessions[index].duration_minutes
                if request.spec and index < len(request.spec.sessions)
                else None
            )
            if item.duration_minutes is None and len(set(durations)) == 1:
                item.duration_minutes = int(durations[0])
            if (
                item.duration_minutes != explicit
                and item.duration_minutes not in old_durations
                and str(item.duration_minutes) not in durations
            ):
                item.duration_minutes = None
        recipient = request.recipient or recipient_from(request.message) or state.get("recipient")
        mail_requested = bool(
            state.get("mail_requested") or recipient and extraction.mail_action == "send"
        )
        if (
            extraction.mail_action == "send"
            and extraction.mail_evidence != unquoted(request.message).strip()
        ):
            raise ValueError("current_human_mail_evidence_required")
        pending = {
            "spec": spec.model_dump(mode="json"),
            "recipient": recipient,
            "mail_requested": mail_requested,
        }
        missing = missing_field(spec)
        if missing:
            return self.reply(
                request,
                pending,
                status="clarification",
                message=QUESTIONS[missing],
                missing_field=missing,
            )
        try:
            events = plan_events(spec)
        except (ValueError, KeyError):
            return self.reply(
                request,
                pending,
                status="clarification",
                message=(
                    "La fecha, zona u hora no es válida o resulta ambigua por el "
                    "cambio de horario. Indique otro horario explícito."
                ),
                missing_field="start_time",
                error_code="local_time_invalid",
            )
        if self.calendar is None:
            return self.reply(
                request,
                pending,
                status="error",
                message=(
                    "Calendar todavía no está autorizado. Configure el grant "
                    "independiente de la cuenta de clases; no se usa Gmail como "
                    "reemplazo."
                ),
                error_code="calendar_not_configured",
                pending_plan=pending,
            )
        busy = await self.calendar.availability(
            min(e.start for e in events), max(e.end for e in events)
        )
        if overlaps(events, busy):
            return self.reply(
                request,
                pending,
                status="conflict",
                message=(
                    "Hay un solapamiento con Calendar. Indique otra fecha u hora; no "
                    "se crearon eventos."
                ),
                error_code="calendar_conflict",
            )
        if extraction.intent == "availability":
            return self.reply(
                request,
                pending,
                status="completed",
                message=(
                    "No hay solapamientos en los intervalos consultados. La "
                    "disponibilidad puede cambiar; no se crearon eventos."
                ),
            )
        preview = AgendaPreview(
            version=request.plan_version or 1,
            request_id=request.request_id,
            operation_id=request.operation_id,
            plan_id=str(uuid4()),
            session_id=request.session_id,
            events=events,
            recipient=recipient,
            mail_requested=mail_requested,
            expires_at=int(self.clock()) + 300,
        )
        return self.reply(
            request,
            {"preview": preview.model_dump(mode="json")},
            status="preview",
            preview=preview,
            message=(
                "Calendar no presenta solapamientos ahora. Revise fecha, zona, "
                "duración y actividades. Responda «Sí, agéndalo» dentro de cinco "
                "minutos para crear estos eventos sin invitados. Un resumen por "
                "correo solo se delegará si lo solicitó."
            ),
        )

    async def readback(self, preview, ids):
        verified = []
        for event, event_id in zip(preview.events, ids, strict=True):
            data = await self.calendar.get(event_id)
            if (
                not data
                or data.get("id") != event_id
                or data.get("status") == "cancelled"
                or data.get("summary") != event.title
                or data.get("extendedProperties", {}).get("private", {}).get("agendaPlan")
                != preview.plan_id
            ):
                return None
            start = datetime.fromisoformat(data["start"]["dateTime"])
            end = datetime.fromisoformat(data["end"]["dateTime"])
            link = data.get("htmlLink", "")
            if (
                start.tzinfo is None
                or end.tzinfo is None
                or start.astimezone(UTC) != event.start.astimezone(UTC)
                or end.astimezone(UTC) != event.end.astimezone(UTC)
                or not link.startswith(
                    ("https://www.google.com/calendar/", "https://calendar.google.com/")
                )
            ):
                return None
            verified.append(
                CreatedEvent(id=event_id, title=event.title, start=start, end=end, link=link)
            )
        return verified

    async def summary_followup(self, request, state):
        events = [CreatedEvent.model_validate(item) for item in state["accepted_events"]]
        email = None
        if request.message.strip():
            try:
                if self.email_peer is None:
                    raise ValueError("email_peer_unavailable")
                content = state.get("summary_content") or {
                    "subject": "Agenda confirmada",
                    "text": "\n".join(
                        f"{event.title}: {event.start.isoformat()} a "
                        f"{event.end.isoformat()}\n{event.link}"
                        for event in events
                    ),
                }
                email = await self.email_peer(
                    EmailRequest(
                        message=request.message,
                        recipient=request.recipient
                        or recipient_from(request.message)
                        or state.get("recipient"),
                        request_id=request.request_id,
                        plan_version=request.plan_version,
                        operation_id=child_operation(request.operation_id, "summary:message"),
                        session_id=request.session_id,
                        content={key: content[key] for key in ("subject", "text")},
                    )
                )
                if email.draft:
                    state["summary_content"] = email.draft.model_dump()
                if email.preview:
                    state["summary_content"] = {
                        key: getattr(email.preview, key) for key in ("subject", "text")
                    }
                state["recipient"] = (
                    request.recipient or recipient_from(request.message) or state.get("recipient")
                )
            except Exception:
                return self.reply(
                    request,
                    state,
                    status="error",
                    events=events,
                    error_code="email_outcome_unknown",
                    email={"status": "delivery_unknown"},
                    message="Los eventos aceptados se conservan. El resultado del correo es "
                    "incierto; no se reintentó.",
                )
        return self.reply(
            request,
            state,
            status="created",
            events=events,
            email=email.model_dump(mode="json") if email else None,
            message="Los eventos aceptados se conservan. "
            "El resultado del correo figura por separado.",
        )

    async def confirm(self, preview, context):
        if self.calendar is None or self.clock() >= preview.expires_at:
            return AgendaResponse(
                status="error",
                message="La propuesta venció o falta Calendar; no se crearon eventos.",
                error_code="approval_invalid",
            )
        key = hashlib.sha256(canonical(preview.model_dump(mode="json"))).hexdigest()
        ids = [
            hashlib.sha256(f"{key}:{index}".encode()).hexdigest()
            for index in range(len(preview.events))
        ]
        fresh, status = self.ledger.reserve(key, ids, preview.events)
        if status == "conflict" or status == "rejected":
            return AgendaResponse(
                status="conflict",
                message=(
                    "La propuesta entra en conflicto con otra reserva. Solicite una "
                    "nueva propuesta."
                ),
                error_code="calendar_conflict",
            )
        try:
            if fresh:
                busy = await self.calendar.availability(
                    min(e.start for e in preview.events), max(e.end for e in preview.events)
                )
                if overlaps(preview.events, busy):
                    self.ledger.complete(key, "rejected")
                    return AgendaResponse(
                        status="conflict",
                        message=(
                            "La disponibilidad cambió desde la propuesta; no se crearon "
                            "eventos. Solicite otro horario."
                        ),
                        error_code="calendar_conflict",
                    )
                for event_id, event in zip(ids, preview.events, strict=True):
                    if self.clock() >= preview.expires_at:
                        raise ValueError("approval_expired")
                    await self.calendar.create(event_id, event, preview.plan_id)
            verified = await self.readback(preview, ids)
        except Exception:
            try:
                verified = await self.readback(preview, ids)
            except Exception:
                verified = None
        if not verified:
            self.ledger.complete(key, "unknown")
            return AgendaResponse(
                status="creation_unknown",
                message=(
                    "No se pudo verificar la creación completa. Puede haber eventos "
                    "parciales. No se repetirán los POST; conserve esta continuación "
                    "para consultar el resultado y revise Calendar."
                ),
                context=context,
                error_code="calendar_creation_unknown",
            )
        self.ledger.complete(key, "created")
        result = AgendaResponse(
            status="created",
            message=(
                "Los eventos fueron creados y comprobados mediante lectura de "
                "Calendar. No se enviaron invitaciones."
            ),
            events=verified,
            context=self.signer.issue(
                preview.session_id,
                {
                    "accepted_events": [item.model_dump(mode="json") for item in verified],
                    "recipient": str(preview.recipient) if preview.recipient else None,
                },
                self.clock(),
            ),
        )
        if preview.mail_requested and preview.recipient and self.ledger.claim_mail(key):
            if self.email_peer is None:
                return result.model_copy(
                    update={
                        "message": (
                            "Calendar confirmó los eventos, pero el agente de correo no está "
                            "configurado."
                        ),
                        "error_code": "email_peer_unavailable",
                    }
                )
            summary = "\n".join(
                f"{event.title}: {event.start.isoformat()} a {event.end.isoformat()}\n{event.link}"
                for event in verified
            )
            try:
                draft = await self.email_peer(
                    EmailRequest(
                        request_id=preview.request_id,
                        plan_version=preview.version,
                        operation_id=child_operation(preview.operation_id, "email:draft"),
                        action="draft",
                        recipient=preview.recipient,
                        content={"subject": "Agenda confirmada", "text": summary},
                        session_id=preview.session_id,
                    )
                )
                if (
                    draft.status != "preview"
                    or not draft.preview
                    or not draft.approval_token
                    or str(draft.preview.recipient) != str(preview.recipient)
                    or draft.preview.text != summary
                ):
                    raise ValueError("email_summary_mismatch")
                email = await self.email_peer(
                    EmailRequest(
                        request_id=preview.request_id,
                        plan_version=preview.version,
                        operation_id=child_operation(preview.operation_id, "email:send"),
                        action="send",
                        preview=draft.preview,
                        approval_token=draft.approval_token,
                        confirmed=True,
                        session_id=preview.session_id,
                    )
                )
                result.email = email.model_dump(mode="json")
                if email.status != "sent":
                    result.error_code = "email_not_confirmed"
                    result.message += " El envío del resumen no está confirmado."
            except Exception:
                result.error_code = "email_outcome_unknown"
                result.message += (
                    " No se pudo confirmar el resultado del correo; no se reintentará."
                )
        return result
