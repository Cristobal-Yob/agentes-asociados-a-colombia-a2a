import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from common.private import configured_key


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
    if parts.scheme == "http" and parts.hostname not in {
        "localhost",
        "127.0.0.1",
        "email",
        "agenda",
    }:
        raise ValueError(
            "HTTP sin cifrar solo se permite para los hosts locales explícitos de la demostración"
        )
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    model_mode: str = "fixture"
    vertex_project: str = ""
    vertex_location: str = "global"
    model_id: str = "gemini-3.1-flash-lite"
    email_transport: str = "mock"
    resend_api_key: str = field(default="", repr=False)
    email_sender: str = "classroom@example.invalid"
    real_email_enabled: bool = False
    approval_secret: str = field(default="", repr=False)
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
    agenda_approval_secret: str = field(default="", repr=False)
    calendar_credentials_file: str = ""
    calendar_ledger_path: str = field(
        default_factory=lambda: str(
            Path(__file__).resolve().parents[1].parent.parent / "work" / "calendar-dispatch.sqlite"
        )
    )
    agenda_peer_url: str = "http://127.0.0.1:8003"
    auth_required: bool = True
    service_api_key: str = field(default="", repr=False)
    email_peer_api_key: str = field(default="", repr=False)
    agenda_peer_api_key: str = field(default="", repr=False)
    workflow_db_path: str = ""
    workflow_store: str = "sqlite"
    workflow_firestore_project: str = ""
    workflow_firestore_database: str = ""

    @classmethod
    def offline(cls, **overrides):
        """Explicit deterministic profile for hermetic tests and offline exercises."""
        values = {
            "model_mode": "fixture",
            "email_transport": "mock",
            "email_sender": "classroom@example.invalid",
            "real_email_enabled": False,
            "auth_required": False,
        }
        values.update(overrides)
        return cls(**values)

    @classmethod
    def from_env(cls, agent: str):
        mode = os.getenv("MODEL_MODE", "fixture")
        transport = os.getenv("EMAIL_TRANSPORT", "mock")
        if agent == "email" and transport == "gmail":
            from email_agent.gmail_auth import require_live_identity

            require_live_identity()
        if mode not in {"fixture", "vertex"} or transport not in {
            "mock",
            "resend",
            "gmail",
            "unconfigured",
        }:
            raise ValueError("Modo de modelo o transporte de correo no admitido")
        workflow_store = os.getenv("WORKFLOW_STORE", "sqlite")
        project = os.getenv("WORKFLOW_FIRESTORE_PROJECT", "")
        database = os.getenv("WORKFLOW_FIRESTORE_DATABASE", "")
        if workflow_store not in {"sqlite", "firestore"}:
            raise ValueError("Almacén durable no admitido")
        if workflow_store == "firestore" and (
            not project or not database or database == "(default)"
        ):
            raise ValueError("Firestore exige proyecto y base de datos dedicada explícitos")
        secret = os.getenv("APPROVAL_SIGNING_KEY", "")
        return cls(
            model_mode=mode,
            service_api_key=configured_key("AGENT_API_KEY"),
            email_peer_api_key=configured_key("EMAIL_AGENT_API_KEY")
            if agent in {"tourism", "agenda"}
            else "",
            agenda_peer_api_key=configured_key("AGENDA_AGENT_API_KEY")
            if agent == "tourism"
            else "",
            workflow_store=workflow_store,
            workflow_firestore_project=project,
            workflow_firestore_database=database,
            workflow_db_path=""
            if workflow_store == "firestore"
            else os.getenv(
                "WORKFLOW_DB_PATH",
                str(
                    Path(__file__).resolve().parents[1].parent.parent
                    / "work"
                    / "workflows"
                    / (agent + ".sqlite")
                ),
            ),
            vertex_project=os.getenv("VERTEX_PROJECT_ID", ""),
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
                else os.getenv("GMAIL_EXPECTED_EMAIL", "")
                if transport == "gmail"
                else "",
            ),
            gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "")
            if agent == "email"
            else "",
            gmail_ledger=os.getenv("GMAIL_LEDGER", "gcs" if os.getenv("K_SERVICE") else "sqlite"),
            gmail_ledger_path=os.getenv("GMAIL_LEDGER_PATH", cls().gmail_ledger_path),
            gmail_ledger_bucket=os.getenv("GMAIL_LEDGER_BUCKET", ""),
            real_email_enabled=os.getenv("ENABLE_REAL_EMAIL", "false") == "true",
            approval_secret=secret,
            agenda_approval_secret=os.getenv("AGENDA_APPROVAL_SIGNING_KEY", "")
            if agent == "agenda"
            else "",
            calendar_credentials_file=os.getenv("CALENDAR_CREDENTIALS_FILE", "")
            if agent == "agenda"
            else "",
            calendar_ledger_path=os.getenv("CALENDAR_LEDGER_PATH", cls().calendar_ledger_path),
            agenda_peer_url=checked_base(os.getenv("AGENDA_AGENT_URL", "http://127.0.0.1:8003")),
            peer_url=checked_base(os.getenv("EMAIL_AGENT_URL", "http://127.0.0.1:8002")),
            public_url=checked_base(
                os.getenv(
                    "PUBLIC_AGENT_URL",
                    f"http://127.0.0.1:{
                        8001 if agent == ('tourism') else 8003 if agent == 'agenda' else 8002
                    }",
                )
            ),
            peer_iam=os.getenv("PEER_USE_IAM") == "true",
            mcp_enabled=os.getenv("ENABLE_MCP") == "true",
            model_call_limit=max(1, min(1000, int(os.getenv("MODEL_CALL_LIMIT", "100")))),
        )
