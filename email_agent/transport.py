from email.utils import parseaddr

import httpx

from common.contracts import EmailResponse


class MailTransport:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client
        self.gmail = None
        self.config_digest = ""
        if settings.email_transport == "gmail":
            from email_agent.gmail_transport import GmailTransport

            self.gmail = GmailTransport(settings, client)
            self.config_digest = self.gmail.config_digest

    async def send(self, preview, *, approval_expires_at=None, clock=None):
        if self.gmail:
            return await self.gmail.send(
                preview, approval_expires_at=approval_expires_at, clock=clock
            )
        if self.settings.email_transport == "mock":
            return EmailResponse(
                status="simulated",
                message="Envío simulado en modo de prueba; no se entregó ningún mensaje.",
                delivery="simulated_only",
            )
        settings = self.settings
        if (
            not settings.real_email_enabled
            or not settings.resend_api_key
            or not settings.email_sender
        ):
            return EmailResponse(
                status="error",
                message="La configuración de correo real está incompleta o no está habilitada.",
                error_code="provider_config",
            )
        sender = parseaddr(settings.email_sender)[1]
        if "@" not in sender or "\n" in settings.email_sender or "\r" in settings.email_sender:
            return EmailResponse(
                status="error",
                message="La configuración del remitente no es válida.",
                error_code="provider_config",
            )
        domain = sender.rsplit("@", 1)[1].lower()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=8, follow_redirects=False)
        headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
        try:
            try:
                verified = await client.get("https://api.resend.com/domains", headers=headers)
                verified.raise_for_status()
                data = verified.json().get("data", [])
                if not any(
                    item.get("name", "").lower() == domain and item.get("status") == "verified"
                    for item in data
                ):
                    return EmailResponse(
                        status="error",
                        message="El proveedor no ha verificado el dominio del remitente.",
                        error_code="unverified_sender",
                    )
            except (httpx.HTTPError, ValueError, TypeError):
                return EmailResponse(
                    status="error",
                    message="No se pudo verificar el dominio del remitente; "
                    "no se intentó el envío.",
                    error_code="verification_unavailable",
                )
            # Resend owns atomic deduplication across process restarts and replicas (24h).
            headers["Idempotency-Key"] = f"colombia-{preview.draft_id}"
            try:
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers=headers,
                    json={
                        "from": preview.sender,
                        "to": [str(preview.recipient)],
                        "subject": preview.subject,
                        "text": preview.text,
                    },
                )
                if response.status_code in {408, 409, 429} or response.status_code >= 500:
                    raise httpx.ReadTimeout("Resultado del proveedor desconocido")
                response.raise_for_status()
                accepted = response.json()
                if not isinstance(accepted, dict) or not accepted.get("id"):
                    raise ValueError("Falta el identificador de aceptación del proveedor")
                return EmailResponse(
                    status="sent",
                    message="El proveedor aceptó el mensaje; no se garantiza su llegada al buzón.",
                    delivery="provider_accepted",
                )
            except httpx.HTTPStatusError:
                return EmailResponse(
                    status="error",
                    message="El proveedor rechazó el mensaje.",
                    error_code="provider_rejected",
                )
            except (httpx.HTTPError, ValueError):
                return EmailResponse(
                    status="delivery_unknown",
                    message=(
                        "Se desconoce el resultado del proveedor. Para reintentar manualmente, "
                        "reutilice esta vista previa aprobada, sin modificar y antes de que venza."
                    ),
                    delivery="unknown",
                    error_code="provider_outcome_unknown",
                )
        finally:
            if owned:
                await client.aclose()
