from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictBool

from agenda_agent.contracts import AgendaResponse
from common.correlation import Correlation, ResultCorrelation


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TripContext(Contract, Correlation):
    model_config = ConfigDict(title="Contexto del viaje")
    city: str | None = Field(
        None,
        max_length=100,
        title="Ciudad",
        description="Ciudad que se desea visitar.",
        examples=["Bogotá"],
    )
    days: int | None = Field(
        None,
        ge=1,
        le=7,
        title="Días",
        description="Duración de la visita, de 1 a 7 días.",
        examples=[2],
    )
    interests: list[str] = Field(
        default_factory=list,
        max_length=5,
        title="Intereses",
        description="Intereses del viaje. Los valores del contrato conservan sus nombres "
        "originales.",
        examples=[["culture", "nature"]],
    )
    draft_email: StrictBool = Field(
        False,
        title="Solicitud de borrador pendiente",
        description="Conserva la solicitud explícita de correo durante las aclaraciones. "
        "Nunca autoriza el envío y se desactiva al obtener la vista previa.",
    )
    recipient: EmailStr | None = Field(
        None,
        title="Destinatario indicado",
        description="Destinatario explícito conservado entre turnos; no se infiere de fuentes.",
        examples=["persona@example.com"],
    )
    email_context: str | None = Field(
        None,
        max_length=24000,
        title="Continuación del correo",
        description="Contexto opaco del agente de correo. Devuelva context sin interpretarlo.",
    )
    agenda_requested: StrictBool = Field(False, title="Solicitud explícita de agenda")
    agenda_context: str | None = Field(None, max_length=24000, title="Continuación opaca de Agenda")


class Source(Contract):
    model_config = ConfigDict(title="Fuente")
    id: str = Field(title="Identificador", description="Identificador original de la fuente.")
    name: str = Field(
        title="Nombre", description="Nombre de la actividad en el catálogo de origen."
    )
    city: str = Field(title="Ciudad")
    url: str = Field(title="Enlace", description="Enlace de referencia de la fuente.")
    retrieved_at: str | None = Field(
        title="Fecha de consulta",
        description="Fecha de consulta en vivo; nula para datos de ejemplo.",
    )
    fixture_as_of: str | None = Field(None, title="Fecha de los datos de ejemplo")
    source_reviewed_at: str | None = Field(None, title="Fecha de revisión de la fuente")
    provenance: Literal["curated_fixture", "live_mcp"] = Field(
        title="Procedencia", description="Datos de ejemplo revisados o registros de MCP en vivo."
    )


class Day(Contract):
    model_config = ConfigDict(title="Día del itinerario")
    day: int = Field(title="Día")
    activities: list[str] = Field(title="Actividades")
    source_ids: list[str] = Field(title="Identificadores de las fuentes")


class Itinerary(Contract):
    model_config = ConfigDict(title="Itinerario")
    city: str = Field(title="Ciudad")
    days: list[Day] = Field(title="Días y actividades")
    summary: str = Field(max_length=3000, title="Resumen")
    sources: list[Source] = Field(title="Fuentes")
    warnings: list[str] = Field(title="Advertencias", description="Límites de las sugerencias.")
    model_mode: str = Field(title="Modo del modelo", description="Modo de ejecución utilizado.")


class EmailPreview(Contract):
    plan_version: int = Field(1, ge=1, title="Versión aprobada")
    model_config = ConfigDict(title="Vista previa del correo")
    draft_id: str = Field(title="Identificador del borrador")
    session_id: str = Field(min_length=1, max_length=100, title="Sesión")
    recipient: EmailStr = Field(title="Destinatario", examples=["student@example.com"])
    subject: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^\r\n]+$",
        title="Asunto",
        description="Asunto exacto incluido en la aprobación firmada.",
        examples=["Tu itinerario en Colombia"],
    )
    text: str = Field(
        min_length=1,
        max_length=12000,
        title="Texto",
        description="Contenido exacto incluido en la aprobación firmada.",
    )
    transport: Literal["mock", "resend", "gmail"] = Field(title="Transporte del correo")
    sender: str = Field(max_length=200, title="Remitente")
    expires_at: int = Field(
        title="Vencimiento",
        description="Instante de vencimiento de la aprobación, en segundos Unix.",
    )


class EmailContent(Contract):
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$", title="Asunto exacto")
    text: str = Field(min_length=1, max_length=12000, title="Texto exacto")


class EmailRequest(Contract, Correlation):
    model_config = ConfigDict(title="Solicitud de correo")
    action: Literal["draft", "send"] = Field(
        "draft",
        title="Acción",
        description="draft prepara una vista previa; send solicita su envío.",
    )
    session_id: str = Field(default="classroom", min_length=1, max_length=100, title="Sesión")
    recipient: EmailStr | None = Field(None, title="Destinatario", examples=["student@example.com"])
    request: str = Field(
        default="",
        max_length=4000,
        title="Solicitud",
        description="Contenido solicitado para el borrador.",
        examples=["Redacta un recordatorio de la reunión de trabajo del lunes."],
    )
    itinerary: Itinerary | None = Field(None, title="Itinerario")
    content: EmailContent | None = Field(
        None,
        title="Contenido exacto opcional",
        description=(
            "Prepara una vista previa del contenido proporcionado; nunca "
            "autoriza por sí solo el envío."
        ),
    )
    preview: EmailPreview | None = Field(
        None, title="Vista previa", description="Vista previa firmada, sin modificar, para enviar."
    )
    approval_token: str | None = Field(
        None,
        max_length=20000,
        title="Token de aprobación",
        description="Token firmado devuelto junto con esta vista previa. No lo modifique.",
    )
    confirmed: StrictBool = Field(
        False,
        title="Confirmación explícita",
        description="Debe ser true para enviar la vista previa exacta antes de su vencimiento. "
        "Preparar un borrador no autoriza el envío.",
    )
    message: str | None = Field(
        None,
        max_length=4000,
        title="Mensaje conversacional",
        description="Entrada conversacional opcional para A2A; conserva el contrato estructurado.",
    )
    context: str | None = Field(None, max_length=24000, title="Contexto opaco de conversación")


class EmailChatRequest(Contract, Correlation):
    model_config = ConfigDict(title="Mensaje al agente de correo")
    message: str = Field(
        min_length=1,
        max_length=4000,
        title="Mensaje",
        description="Converse normalmente o solicite redactar y enviar a un destinatario "
        "explícito. "
        "Una orden explícita de envío con datos suficientes envía en ese mismo turno. "
        "Una solicitud de redacción devuelve asunto y texto sin exigir destinatario, "
        "aprobación ni envío.",
        examples=["Envía a persona@example.com un recordatorio de la reunión de mañana."],
    )
    context: str | None = Field(
        None,
        max_length=24000,
        title="Contexto",
        description="Devuelva el context anterior sin modificar. No necesita interpretarlo.",
    )
    session_id: str | None = Field(
        None,
        min_length=1,
        max_length=100,
        title="Sesión opcional",
        description="Si se indica, debe coincidir con la sesión firmada en context.",
    )


class EmailResponse(Contract, ResultCorrelation):
    model_config = ConfigDict(title="Respuesta del agente de correo")
    status: Literal[
        "preview",
        "simulated",
        "sent",
        "delivery_unknown",
        "error",
        "clarification",
        "completed",
        "cancelled",
    ] = Field(
        title="Estado",
        description="Resultado del procesamiento; sent no garantiza llegada al buzón.",
    )
    message: str = Field(title="Mensaje")
    draft: EmailContent | None = Field(
        None,
        title="Redacción sin envío",
        description="Asunto y texto redactados, sin aprobación ni autorización de envío.",
    )
    preview: EmailPreview | None = Field(None, title="Vista previa")
    approval_token: str | None = Field(None, title="Token de aprobación")
    delivery: str | None = Field(title="Resultado del envío", default=None)
    error_code: str | None = Field(None, title="Código de error")
    context: str | None = Field(None, title="Contexto opaco para el siguiente mensaje")
    conversation_state: (
        Literal["draft", "awaiting_confirmation", "sent", "failed", "completed", "cancelled"] | None
    ) = Field(None, title="Estado de la conversación")


class TourismRequest(Contract, Correlation):
    model_config = ConfigDict(title="Solicitud de itinerario")
    message: str = Field(
        default="",
        max_length=4000,
        title="Mensaje",
        description="Preferencias para la visita y solicitud opcional de correo. "
        "Una orden explícita "
        "de envío permite delegarlo en el mismo turno. Una redacción libre puede delegarse "
        "sin itinerario ni destinatario y no envía ni exige aprobación.",
        examples=["Bogotá, 2 días, cultura; prepáralo por correo para persona@example.com"],
    )
    context: TripContext = Field(default_factory=TripContext, title="Contexto del viaje")
    draft_email: StrictBool = Field(
        False,
        title="Preparar correo",
        description="Solicita una vista previa al agente de correo mediante A2A. "
        "No envía mensajes.",
    )
    recipient: EmailStr | None = Field(None, title="Destinatario", examples=["student@example.com"])
    session_id: str = Field(default="classroom", min_length=1, max_length=100, title="Sesión")


class TourismResponse(Contract, ResultCorrelation):
    model_config = ConfigDict(title="Respuesta del agente de itinerarios")
    status: Literal["completed", "partial", "clarification", "error"] = Field(title="Estado")
    message: str = Field(title="Mensaje")
    context: TripContext = Field(title="Contexto del viaje")
    missing_field: str | None = Field(None, title="Campo pendiente")
    itinerary: Itinerary | None = Field(None, title="Itinerario")
    email: EmailResponse | None = Field(None, title="Respuesta del agente de correo")
    agenda: AgendaResponse | None = Field(None, title="Respuesta del agente de agenda")
    error_code: str | None = Field(None, title="Código de error")
    retrieval: str = Field("not_requested", title="Estado de consulta de fuentes")
    conversation_state: (
        Literal["draft", "awaiting_confirmation", "sent", "failed", "completed", "cancelled"] | None
    ) = Field(None, title="Estado de la conversación")
