import os
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from common.correlation import Correlation, ResultCorrelation


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionSpec(Contract):
    title: str = Field(min_length=1, max_length=200, title="Actividad")
    duration_minutes: int | None = Field(None, ge=1, le=480, title="Duración en minutos")
    day_offset: int = Field(0, ge=0, le=6, title="Días desde la fecha inicial")


class AgendaSpec(Contract):
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", title="Fecha inicial")
    timezone: str | None = Field(None, max_length=100, title="Zona horaria IANA")
    start_time: str | None = Field(None, pattern=r"^\d{2}:\d{2}$", title="Hora local inicial")
    sessions: list[SessionSpec] = Field(default_factory=list, max_length=7, title="Actividades")


class PendingAgendaPlan(Contract):
    spec: AgendaSpec
    recipient: EmailStr | None = None
    mail_requested: bool = False


class AgendaEvent(Contract):
    title: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime
    timezone: str


class AgendaPreview(Contract):
    plan_id: str
    request_id: str | None = None
    operation_id: str | None = None
    session_id: str
    version: int = 1
    calendar_id: Literal["primary"] = "primary"
    account: str = Field(
        default_factory=lambda: os.getenv("GMAIL_EXPECTED_EMAIL", "sender@example.com"),
        min_length=3,
        max_length=254,
    )
    events: list[AgendaEvent] = Field(min_length=1, max_length=7)
    recipient: EmailStr | None = None
    mail_requested: bool = False
    expires_at: int


class CreatedEvent(Contract):
    id: str
    title: str
    start: datetime
    end: datetime
    link: str


class AgendaRequest(Contract, Correlation):
    message: str = Field(default="", max_length=4000, title="Mensaje")
    session_id: str = Field(default="classroom", min_length=1, max_length=100, title="Sesión")
    context: str | None = Field(None, max_length=24000, title="Contexto opaco")
    spec: AgendaSpec | None = Field(None, title="Preferencias estructuradas opcionales")
    recipient: EmailStr | None = Field(None, title="Destinatario del resumen solicitado")


class AgendaResponse(Contract, ResultCorrelation):
    status: Literal[
        "completed", "clarification", "preview", "created", "conflict", "creation_unknown", "error"
    ]
    message: str
    context: str | None = None
    preview: AgendaPreview | None = None
    pending_plan: PendingAgendaPlan | None = None
    events: list[CreatedEvent] = Field(default_factory=list)
    missing_field: str | None = None
    error_code: str | None = None
    email: dict | None = None


class AgendaExtraction(Contract):
    intent: Literal["chat", "plan", "availability"]
    message: str = Field(default="", max_length=3000)
    spec: AgendaSpec = Field(default_factory=AgendaSpec)
    mail_action: Literal["none", "send"] = "none"
    mail_evidence: str = Field(default="", max_length=4000)
