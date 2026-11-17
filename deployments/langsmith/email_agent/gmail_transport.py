import base64
import hashlib
import os
import re
import time
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate

import httpx

from common.contracts import EmailResponse
from email_agent.approval import canonical
from email_agent.dispatch_ledger import GcsLedger, SqliteLedger
from email_agent.gmail_auth import SENDER, OAuthError, authorize_runtime, load_credentials

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def raw_message(preview):
    message = EmailMessage(policy=SMTP)
    message["From"] = SENDER
    message["To"] = str(preview.recipient)
    message["Subject"] = preview.subject
    message["Date"] = formatdate(usegmt=True)
    message["MIME-Version"] = "1.0"
    message.set_type("text/plain")
    message.set_param("charset", "utf-8")
    # Preserve the exact approved text; set_content would add a trailing newline.
    message["Content-Transfer-Encoding"] = "base64"
    message.set_payload(base64.encodebytes(preview.text.encode("utf-8")).decode("ascii"))
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def unknown(code="gmail_delivery_unknown"):
    return EmailResponse(
        status="delivery_unknown",
        delivery="unknown",
        error_code=code,
        message="La entrega es incierta. Este borrador aprobado no volverá a enviarse; "
        "revise manualmente el buzón del remitente antes de considerar un nuevo borrador.",
    )


def expired_approval():
    return EmailResponse(
        status="error",
        delivery="not_attempted",
        error_code="approval_expired",
        message="La aprobación venció antes del envío por Gmail; no se intentó el envío. "
        "Solicite una nueva vista previa y confírmela.",
    )


class GmailTransport:
    def __init__(self, settings, client=None, ledger=None):
        self.settings = settings
        self.client = client
        if settings.email_sender != SENDER:
            raise ValueError(f"El remitente de Gmail debe ser la cuenta aprobada {SENDER}")
        if os.getenv("K_SERVICE") and settings.gmail_ledger != "gcs":
            raise ValueError(
                "Gmail en Cloud Run exige un registro GCS compartido; SQLite está prohibido"
            )
        self.credentials = load_credentials(settings.gmail_credentials_file)
        if settings.gmail_ledger == "sqlite":
            self.ledger = ledger or SqliteLedger(settings.gmail_ledger_path)
            ledger_identity = str(self.ledger.path)
        elif settings.gmail_ledger == "gcs":
            self.ledger = ledger or GcsLedger(settings.gmail_ledger_bucket)
            ledger_identity = settings.gmail_ledger_bucket
        else:
            raise ValueError("Elija sqlite localmente o el registro GCS compartido aprobado")
        self.config_digest = hashlib.sha256(
            canonical(
                {
                    "credentials": self.credentials,
                    "ledger": settings.gmail_ledger,
                    "ledger_identity": ledger_identity,
                }
            )
        ).hexdigest()

    async def send(self, preview, *, approval_expires_at=None, clock=None):
        if (
            not self.settings.real_email_enabled
            or preview.sender != SENDER
            or preview.transport != "gmail"
        ):
            return EmailResponse(
                status="error",
                delivery="not_attempted",
                error_code="provider_config",
                message="La configuración del remitente, transporte o envío real de Gmail "
                "no es válida.",
            )
        if type(approval_expires_at) is not int or approval_expires_at != preview.expires_at:
            return EmailResponse(
                status="error",
                delivery="not_attempted",
                error_code="approval_invalid",
                message="Se exige el vencimiento de la vista previa firmada verificado "
                "por el servicio.",
            )
        clock = clock or time.time

        def still_valid():
            return approval_expires_at - 300 <= clock() < approval_expires_at

        if not still_valid():
            return expired_approval()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=4, follow_redirects=False)
        try:
            try:
                headers = await authorize_runtime(self.credentials, client)
            except OAuthError:
                return EmailResponse(
                    status="error",
                    delivery="not_attempted",
                    error_code="gmail_authorization_failed",
                    message=(
                        "Falló la renovación de OAuth de Gmail o la verificación de los permisos "
                        "o de la cuenta seleccionada; no se intentó el envío."
                    ),
                )
            if not still_valid():
                return expired_approval()
            key = hashlib.sha256(canonical(preview.model_dump(mode="json"))).hexdigest()
            try:
                claim = await self.ledger.reserve(key)
            except Exception:
                return unknown("gmail_ledger_uncertain")
            if not claim.new:
                if claim.status == "sent" and claim.provider_id:
                    return EmailResponse(
                        status="sent",
                        delivery="provider_accepted",
                        message="El proveedor ya había registrado la aceptación; "
                        "no se repitió el envío.",
                    )
                if claim.status == "rejected":
                    return EmailResponse(
                        status="error",
                        delivery="not_accepted",
                        error_code="provider_rejected",
                        message="El proveedor ya había registrado el rechazo; "
                        "este borrador no se reintenta.",
                    )
                return unknown()
            status, provider_id = "unknown", None
            try:
                raw = raw_message(preview)
                # An existing durable reservation is retained if approval expired while waiting.
                if not still_valid():
                    return expired_approval()
                response = await client.post(
                    SEND_URL, headers=headers, json={"raw": raw}, timeout=8
                )
                if 400 <= response.status_code < 500 and response.status_code not in {
                    408,
                    409,
                    429,
                }:
                    status = "rejected"
                else:
                    response.raise_for_status()
                    provider_id = response.json().get("id")
                    if not isinstance(provider_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,200}", provider_id
                    ):
                        raise ValueError("provider_acceptance_invalid")
                    status = "sent"
            except Exception:
                status, provider_id = "unknown", None
            if not await self.ledger.complete(claim, status, provider_id):
                return unknown("gmail_ledger_completion_uncertain")
            if status == "sent":
                return EmailResponse(
                    status="sent",
                    delivery="provider_accepted",
                    message="Gmail aceptó el mensaje; no se garantiza su llegada al buzón.",
                )
            if status == "rejected":
                return EmailResponse(
                    status="error",
                    delivery="not_accepted",
                    error_code="provider_rejected",
                    message="Gmail rechazó el mensaje; este borrador no volverá a enviarse.",
                )
            return unknown()
        finally:
            if owned:
                await client.aclose()
