import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def checked_base(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Se exige una URL absoluta HTTP(S) del servicio")
    if (
        parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("La URL base no debe contener credenciales, ruta, consulta ni fragmento")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "email"}:
        raise ValueError(
            "HTTP sin cifrar solo se permite para los hosts locales explícitos de la demostración"
        )
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    model_mode: str = "vertex"
    vertex_project: str = "example-project"
    vertex_location: str = "global"
    model_id: str = "gemini-3.1-flash-lite"
    email_transport: str = "gmail"
    resend_api_key: str = ""
    email_sender: str = "student@example.com"
    real_email_enabled: bool = True
    approval_secret: str = ""
    peer_url: str = "http://127.0.0.1:8002"
    public_url: str = "http://127.0.0.1:8001"
    peer_iam: bool = False
    mcp_enabled: bool = False
    model_call_limit: int = 100
    gmail_credentials_file: str = ""
    gmail_ledger: str = "sqlite"
    gmail_ledger_path: str = field(
        default_factory=lambda: str(
            Path(__file__).resolve().parents[1].parent.parent / "work" / "gmail-dispatch.sqlite"
        )
    )
    gmail_ledger_bucket: str = ""

    @classmethod
    def offline(cls, **overrides):
        """Explicit deterministic profile for hermetic tests and offline exercises."""
        values = {
            "model_mode": "fixture",
            "email_transport": "mock",
            "email_sender": "classroom@example.invalid",
            "real_email_enabled": False,
        }
        values.update(overrides)
        return cls(**values)

    @classmethod
    def from_env(cls, agent: str):
        mode = os.getenv("MODEL_MODE", "vertex")
        transport = os.getenv("EMAIL_TRANSPORT", "gmail")
        if mode not in {"fixture", "vertex"} or transport not in {
            "mock",
            "resend",
            "gmail",
            "unconfigured",
        }:
            raise ValueError("Modo de modelo o transporte de correo no admitido")
        secret = os.getenv("APPROVAL_SIGNING_KEY", "")
        return cls(
            model_mode=mode,
            vertex_project=os.getenv("VERTEX_PROJECT_ID", "example-project"),
            vertex_location=os.getenv("VERTEX_LOCATION", "global"),
            model_id=os.getenv("MODEL_ID", "gemini-3.1-flash-lite"),
            email_transport=transport,
            resend_api_key=(
                os.getenv("RESEND_API_KEY", "")
                if agent == "email" and transport == "resend"
                else ""
            ),
            email_sender=os.getenv(
                "EMAIL_SENDER",
                "classroom@example.invalid"
                if transport == "mock"
                else "student@example.com"
                if transport == "gmail"
                else "",
            ),
            gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "")
            if agent == "email"
            else "",
            gmail_ledger=os.getenv("GMAIL_LEDGER", "gcs" if os.getenv("K_SERVICE") else "sqlite"),
            gmail_ledger_path=os.getenv("GMAIL_LEDGER_PATH", cls().gmail_ledger_path),
            gmail_ledger_bucket=os.getenv("GMAIL_LEDGER_BUCKET", ""),
            real_email_enabled=os.getenv("ENABLE_REAL_EMAIL", "true") == "true",
            approval_secret=secret,
            peer_url=checked_base(os.getenv("EMAIL_AGENT_URL", "http://127.0.0.1:8002")),
            public_url=checked_base(
                os.getenv(
                    "PUBLIC_AGENT_URL", f"http://127.0.0.1:{8001 if agent == 'tourism' else 8002}"
                )
            ),
            peer_iam=os.getenv("PEER_USE_IAM") == "true",
            mcp_enabled=os.getenv("ENABLE_MCP") == "true",
            model_call_limit=max(1, min(1000, int(os.getenv("MODEL_CALL_LIMIT", "100")))),
        )
