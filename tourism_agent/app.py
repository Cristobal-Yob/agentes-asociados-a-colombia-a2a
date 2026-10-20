from typing import Annotated

from fastapi import Body

from common.a2a_transport import agenda_peer, email_peer, mount_a2a
from common.auth import configure_auth
from common.config import Settings
from common.contracts import TourismRequest, TourismResponse
from common.runtime import base_app
from tourism_agent.catalog import Catalog
from tourism_agent.service import TourismService


def create_app(settings=None, *, service=None):
    settings = settings or Settings.from_env("tourism")
    mcp = None
    if settings.mcp_enabled:
        from tourism_agent.mcp_catalog import McpCatalog

        mcp = McpCatalog()
    service = service or TourismService(
        settings, peer=email_peer(settings), catalog=Catalog(mcp), agenda_peer=agenda_peer(settings)
    )
    app = base_app(
        "Agente de itinerarios en Colombia",
        "Converse en español: el modelo decide automáticamente si responde o utiliza "
        "herramientas para generar un itinerario y consultar a otros agentes. Conserva "
        "historial acotado y plan vigente por request_id y session_id. "
        "Prepara sugerencias basadas en fuentes del catálogo y puede solicitar un borrador "
        "al agente de correo mediante A2A, también sobre temas libres sin itinerario. "
        "Redactar no envía ni exige destinatario o aprobación. Una orden actual suficiente "
        "permite enviar sin otra aprobación. Los ejemplos no reservan actividades "
        "ni verifican precios, horarios o condiciones de seguridad. Puede delegar a Agenda "
        "si solicita agendar; requiere fecha, zona, hora y duración explícitas y confirmación "
        "de la propuesta. El correo posterior contiene solo eventos comprobados en Calendar.",
    )
    app.state.service = service

    @app.post(
        "/api/chat",
        response_model=TourismResponse,
        summary="Consultar o completar un itinerario",
        description="Puede saludar, conversar, solicitar un plan y después pedir correo sin "
        "regenerar el itinerario. El modelo decide si usa herramientas; si faltan datos, "
        "pregunta. Conserve request_id y session_id para continuar, también tras reiniciar, "
        "sin copiar context. plan_version es opcional; si lo envía, use el valor de la "
        "última respuesta. Puede pedir una redacción libre en message sin destinatario ni plan. "
        "draft_email sigue siendo compatible. "
        "Una orden explícita completa de envío permite delegar su ejecución en ese turno; "
        "pedir solo un borrador devuelve asunto y texto sin enviar ni pedir aprobación.",
        tags=["Itinerarios"],
        response_description="Itinerario, solicitud de aclaración o error",
    )
    async def chat(
        request: Annotated[
            TourismRequest,
            Body(
                openapi_examples={
                    "01_greeting": {
                        "summary": "1. Saludar",
                        "description": "Ejecute primero este paso. Conserve los mismos IDs para "
                        "los pasos 2 y 3. Para otra conversación, cambie request_id.",
                        "value": {
                            "message": "Hola",
                            "request_id": "charla-student-01",
                            "session_id": "student-local",
                        },
                    },
                    "02_name": {
                        "summary": "2. Presentarse",
                        "description": "Después del paso 1, ejecute este ejemplo sin cambiar "
                        "request_id ni session_id. Para otra conversación, cambie request_id.",
                        "value": {
                            "message": "Me llamo Student",
                            "request_id": "charla-student-01",
                            "session_id": "student-local",
                        },
                    },
                    "03_recall_name": {
                        "summary": "3. Recordar el nombre",
                        "description": "Después del paso 2, pregunte con los mismos IDs de "
                        "los pasos 1 y 2. Para otra conversación, cambie request_id.",
                        "value": {
                            "message": "¿Cómo me llamo?",
                            "request_id": "charla-student-01",
                            "session_id": "student-local",
                        },
                    },
                    "primera_solicitud": {
                        "summary": "Primer mensaje, sin contexto previo",
                        "value": {
                            "message": "Bogotá, 2 días, cultura; prepara el itinerario "
                            "por correo para persona@example.com."
                        },
                    },
                    "redaccion_libre": {
                        "summary": "Redacción libre por Correo, sin itinerario ni envío",
                        "value": {"message": "Redacta un correo amable para reagendar una clase."},
                    },
                    "agenda_y_correo": {
                        "summary": "Proponer agenda del itinerario y resumen por correo",
                        "value": {
                            "message": (
                                "Bogotá, 3 días, cultura; agenda desde 2030-05-06 a las 10:00 en "
                                "America/Bogota, 60 minutos por día y envía resumen a "
                                "persona@example.com."
                            )
                        },
                    },
                }
            ),
        ],
    ):
        return await service.handle(request)

    mount_a2a(
        app,
        service,
        TourismRequest,
        settings,
        "Agente de itinerarios en Colombia",
        "Conversación nativa del modelo, plan vigente y preparación opcional de correo "
        "mediante A2A. "
        "Redactar no envía ni exige destinatario. Solo Correo ejecuta el envío ante una orden "
        "humana actual suficiente, sin pedir otra aprobación y con validaciones técnicas.",
        [
            (
                "itinerary",
                "Planificar una visita a Colombia",
                "Aclara ciudad, duración e intereses; "
                "selecciona únicamente registros del catálogo.",
            ),
            (
                "agenda",
                "Delegar a Agenda",
                (
                    "Conserva la propuesta firmada de Agenda y delega su confirmación "
                    "específica; correo solo después de crear eventos verificados."
                ),
            ),
        ],
    )
    configure_auth(app, settings, service)
    return app
