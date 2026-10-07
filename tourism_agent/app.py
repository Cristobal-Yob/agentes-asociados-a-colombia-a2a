from typing import Annotated

from fastapi import Body

from common.a2a_transport import email_peer, mount_a2a
from common.config import Settings
from common.contracts import TourismRequest, TourismResponse
from common.runtime import base_app
from tourism_agent.catalog import Catalog
from tourism_agent.service import TourismService


def create_app(settings=None):
    settings = settings or Settings.from_env("tourism")
    mcp = None
    if settings.mcp_enabled:
        from tourism_agent.mcp_catalog import McpCatalog

        mcp = McpCatalog()
    service = TourismService(settings, peer=email_peer(settings), catalog=Catalog(mcp))
    app = base_app(
        "Agente de itinerarios en Colombia",
        "Prepara sugerencias basadas en fuentes del catálogo y puede solicitar un borrador "
        "al agente de correo mediante A2A. Tras revisar la vista previa, puede delegar su "
        "confirmación explícita al agente de correo. Los ejemplos no reservan actividades "
        "ni verifican precios, horarios o condiciones de seguridad.",
    )
    app.state.service = service

    @app.post(
        "/api/chat",
        response_model=TourismResponse,
        summary="Consultar o completar un itinerario",
        description="Indique ciudad, duración e intereses. Si falta información, el agente "
        "solicita un dato por vez. Puede pedir correo en el propio message con destinatario "
        "explícito. Devuelva context intacto en el turno siguiente; después de revisar la "
        "vista previa responda «Sí, envíalo». draft_email sigue siendo compatible. "
        "Una orden explícita completa de envío permite delegar su ejecución en ese turno; "
        "pedir solo un borrador conserva la confirmación posterior.",
        tags=["Itinerarios"],
        response_description="Itinerario, solicitud de aclaración o error",
    )
    async def chat(
        request: Annotated[
            TourismRequest,
            Body(
                openapi_examples={
                    "primera_solicitud": {
                        "summary": "Primer mensaje, sin contexto previo",
                        "value": {
                            "message": "Bogotá, 2 días, cultura; prepara el itinerario "
                            "por correo para persona@example.com."
                        },
                    }
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
        "Sugerencias basadas en fuentes y preparación opcional de correo mediante A2A. "
        "Solo el agente de correo ejecuta el envío tras confirmar una vista previa exacta.",
        [
            (
                "itinerary",
                "Planificar una visita a Colombia",
                "Aclara ciudad, duración e intereses; "
                "selecciona únicamente registros del catálogo.",
            )
        ],
    )
    return app
