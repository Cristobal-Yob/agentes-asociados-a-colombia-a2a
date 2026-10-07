from typing import Annotated

from fastapi import Body

from common.a2a_transport import mount_a2a
from common.config import Settings
from common.contracts import EmailChatRequest, EmailRequest, EmailResponse
from common.runtime import base_app
from email_agent.service import EmailService


def create_app(settings=None):
    settings = settings or Settings.from_env("email")
    service = EmailService(settings)
    app = base_app(
        "Agente independiente de correo",
        "Redacta correos de cualquier tema a partir de una solicitud directa y un destinatario "
        "explícito; también puede recibir un itinerario. No requiere conectarse a turismo. "
        "Una orden explícita de redactar y enviar con datos suficientes permite enviar desde "
        "student@example.com en ese turno. Los borradores requieren una confirmación posterior. "
        "La aceptación del proveedor no garantiza "
        "la llegada al buzón.",
    )
    app.state.service = service

    @app.post(
        "/api/chat",
        response_model=EmailResponse,
        tags=["Conversación"],
        summary="Conversar, redactar y enviar desde su Gmail personal",
        description="Envíe solo message. Si la respuesta incluye context, devuélvalo intacto "
        "junto con el siguiente message. Una orden explícita de envío con destinatario y contenido "
        "suficientes redacta y envía en el mismo turno. Pedir solo redactar devuelve una vista "
        "previa; en ese caso «Sí, envíalo» con context confirma el texto exacto.",
        response_description="Aclaración, vista previa pendiente de confirmación o resultado real",
        responses={
            200: {
                "content": {
                    "application/json": {
                        "examples": {
                            "vista_previa": {
                                "summary": "Vista previa ilustrativa, sin autorización ni envío",
                                "value": {
                                    "status": "preview",
                                    "message": "Ejemplo ilustrativo: revise la vista previa real "
                                    "y devuelva su context antes de confirmar. "
                                    "Este ejemplo no contiene una aprobación válida.",
                                    "conversation_state": "awaiting_confirmation",
                                    "preview": {
                                        "draft_id": "BORRADOR_ILUSTRATIVO",
                                        "session_id": "SESION_ILUSTRATIVA",
                                        "recipient": "persona@example.com",
                                        "subject": "Recordatorio de reunión",
                                        "text": "Le recordamos la reunión de mañana.",
                                        "transport": "gmail",
                                        "sender": "REMITENTE_CONFIGURADO",
                                        "expires_at": 0,
                                    },
                                },
                            }
                        }
                    }
                }
            }
        },
    )
    async def chat(
        request: Annotated[
            EmailChatRequest,
            Body(
                openapi_examples={
                    "primera_solicitud": {
                        "summary": "Primer mensaje, sin contexto previo",
                        "value": {
                            "message": "Envía a persona@example.com un recordatorio "
                            "de la reunión de mañana."
                        },
                    }
                }
            ),
        ],
    ):
        return await service.chat(request)

    @app.post(
        "/api/email",
        response_model=EmailResponse,
        summary="Preparar o confirmar un correo",
        description="action=draft prepara una vista previa. Para action=send, use la misma "
        "sesión, la vista previa y el token firmados sin modificarlos, y confirmed=true "
        "antes del vencimiento. Una solicitud de borrador nunca autoriza un envío.",
        tags=["Correo"],
        response_description="Vista previa, resultado del envío, solicitud de aclaración o error",
    )
    async def email(request: EmailRequest):
        return await service.handle(request)

    mount_a2a(
        app,
        service,
        EmailRequest,
        settings,
        "Agente independiente de correo",
        "Prepara un borrador sobre cualquier tema solicitado sin depender de turismo; "
        "envía desde student@example.com ante una orden humana explícita suficiente, "
        "o permite confirmar posteriormente una vista previa firmada.",
        [
            (
                "draft",
                "Preparar la vista previa de un mensaje",
                "Produce una vista previa firmada del destinatario, el asunto y el texto.",
            ),
            (
                "send",
                "Confirmar una vista previa",
                "Exige confirmación explícita y una aprobación vigente sin modificaciones.",
            ),
        ],
    )
    return app
