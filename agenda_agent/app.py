from typing import Annotated

from fastapi import Body

from agenda_agent.contracts import AgendaRequest, AgendaResponse
from agenda_agent.service import AgendaService
from common.a2a_transport import email_peer, mount_a2a
from common.auth import configure_auth
from common.config import Settings
from common.runtime import base_app


def create_app(settings=None, *, service=None):
    settings = settings or Settings.from_env("agenda")
    service = service or AgendaService(settings, email_peer=email_peer(settings))
    app = base_app(
        "Agente independiente de agenda",
        (
            "Organiza actividades y consulta Google Calendar de la cuenta de "
            "clases. Crear eventos exige revisar una propuesta exacta y "
            "confirmarla. No envía invitaciones; correo es una delegación "
            "opcional."
        ),
        health_state=lambda: {
            "status": "ok" if service.calendar else "degraded",
            "calendar": "configured_not_checked" if service.calendar else "not_authorized",
        },
    )
    app.state.service = service

    @app.post(
        "/api/chat",
        response_model=AgendaResponse,
        summary="Conversar, consultar o proponer una agenda",
        tags=["Agenda"],
    )
    async def chat(
        request: Annotated[
            AgendaRequest,
            Body(
                openapi_examples={
                    "sesiones": {
                        "summary": "Proponer sesiones con datos explícitos",
                        "value": {
                            "message": (
                                "Organiza repaso y práctica el 2030-05-06 desde las 10:00 en "
                                "America/Bogota, 60 minutos cada sesión."
                            )
                        },
                    },
                    "confirmar": {
                        "summary": "Confirmar la propuesta vigente",
                        "value": {
                            "message": "Sí, agéndalo",
                            "context": "CONTEXTO_REAL_DEVUELTO",
                            "session_id": "classroom",
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
        AgendaRequest,
        settings,
        "Agente independiente de agenda",
        (
            "Consulta disponibilidad, propone actividades y crea eventos solo "
            "tras una aprobación humana específica. Calendar tiene "
            "autorización independiente de Gmail."
        ),
        [
            (
                "agenda",
                "Organizar actividades",
                (
                    "Aclara fecha, zona horaria y duración; consulta Calendar y "
                    "devuelve una propuesta."
                ),
            ),
            (
                "create",
                "Confirmar eventos",
                "Exige la continuación firmada y vuelve a comprobar disponibilidad antes de crear.",
            ),
        ],
    )
    configure_auth(app, settings, service)
    return app
