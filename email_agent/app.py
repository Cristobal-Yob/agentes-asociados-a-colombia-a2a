from typing import Annotated

from fastapi import Body

from common.a2a_transport import mount_a2a
from common.auth import configure_auth
from common.config import Settings
from common.contracts import EmailChatRequest, EmailRequest, EmailResponse
from common.runtime import base_app
from email_agent.service import EmailService


def create_app(settings=None):
    settings = settings or Settings.from_env("email")
    service = EmailService(settings)
    app = base_app(
        "Agente independiente de correo",
        "Redacta correos de cualquier tema sin exigir destinatario ni aprobación; "
        "también puede recibir un itinerario. No requiere conectarse a turismo. "
        "Una orden explícita de redactar y enviar con datos suficientes permite enviar desde "
        "su cuenta configurada en ese turno. "
        "Redactar no envía. Enviar una redacción vigente exige una orden humana actual, "
        "sin otro paso de aprobación. "
        "La aceptación del proveedor no garantiza "
        "la llegada al buzón.",
    )
    app.state.service = service

    @app.post(
        "/api/chat",
        response_model=EmailResponse,
        tags=["Conversación"],
        summary="Conversar, redactar y enviar desde el Gmail de clases",
        description="Envíe message y conserve los IDs devueltos para continuar. Pedir redactar "
        "devuelve asunto y texto sin exigir destinatario ni aprobación y sin enviar. Una orden "
        "explícita actual de envío con datos suficientes envía en ese turno, sin pedir otra "
        "aprobación. Para enviar una redacción anterior conserve request_id, session_id y la "
        "plan_version actual. No necesita copiar context.",
        response_description="Redacción lista, respuesta conversacional o resultado real del envío",
        responses={
            200: {
                "content": {
                    "application/json": {
                        "examples": {
                            "redaccion": {
                                "summary": "Redacción ilustrativa, sin aprobación ni envío",
                                "value": {
                                    "status": "completed",
                                    "message": "Asunto: Recordatorio de reunión\n\n"
                                    "Le recordamos la reunión de mañana.",
                                    "conversation_state": "draft",
                                    "delivery": "not_attempted",
                                    "draft": {
                                        "subject": "Recordatorio de reunión",
                                        "text": "Le recordamos la reunión de mañana.",
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
                    "redaccion_libre": {
                        "summary": "Redactar sin destinatario ni envío",
                        "value": {"message": "Redacta un correo amable para reagendar una clase."},
                    },
                    "primera_solicitud": {
                        "summary": "Primer mensaje, sin contexto previo",
                        "value": {
                            "message": "Envía a persona@example.com un recordatorio "
                            "de la reunión de mañana."
                        },
                    },
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
        "envía desde sender@example.com ante una orden humana explícita suficiente, "
        "sin exigir otra aprobación. Redactar no envía ni exige destinatario. "
        "El contrato tipado conserva la validación de sus firmas técnicas.",
        [
            (
                "draft",
                "Redactar un mensaje",
                "En conversación devuelve asunto y texto sin destinatario ni envío. "
                "El contrato tipado permite preparar una vista previa firmada.",
            ),
            (
                "send",
                "Enviar por orden explícita",
                "Una orden conversacional actual suficiente permite enviar en ese turno. "
                "El contrato tipado verifica su vista previa y firma vigentes.",
            ),
        ],
    )
    configure_auth(app, settings, service)
    return app
