import asyncio
import json
from pathlib import Path

SENDER = "student@example.com"
SCOPES = {"openid", "email", "https://www.googleapis.com/auth/gmail.send"}
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class OAuthError(ValueError):
    pass


def load_credentials(path):
    try:
        target = Path(path).resolve()
        project = Path(__file__).resolve().parents[1]
        if target.is_relative_to(project) or not target.is_file() or target.stat().st_size > 16384:
            raise OAuthError("gmail_credential_file_invalid")
        data = json.loads(target.read_text(encoding="utf-8"))
        validate_credentials(data)
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise OAuthError(
            "Faltan las credenciales de Gmail o no son válidas. Ejecute el asistente OAuth de "
            "escritorio aprobado y configure GMAIL_CREDENTIALS_FILE con el archivo protegido "
            "fuera de este proyecto."
        ) from None


def normalized_scopes(value):
    scopes = value.split() if isinstance(value, str) else value
    if not isinstance(scopes, (list, set, tuple)) or any(not isinstance(s, str) for s in scopes):
        raise OAuthError("gmail_scope_invalid")
    return {"email" if s == "https://www.googleapis.com/auth/userinfo.email" else s for s in scopes}


def validate_credentials(data):
    if (
        not isinstance(data, dict)
        or data.get("type") != "authorized_user"
        or data.get("project_id") != "example-project"
        or data.get("account_email") != SENDER
        or data.get("token_uri") != TOKEN_URL
        or normalized_scopes(data.get("scopes")) != SCOPES
        or not str(data.get("client_id", "")).endswith(".apps.googleusercontent.com")
        or any(
            not isinstance(data.get(key), str) or not 1 <= len(data[key]) <= 4096
            for key in ["client_id", "client_secret", "refresh_token", "account_sub"]
        )
    ):
        raise OAuthError("gmail_credentials_invalid")


def verify_identity(info, expected_sub):
    if (
        not isinstance(info, dict)
        or info.get("email") != SENDER
        or info.get("email_verified") is not True
        or not expected_sub
        or info.get("sub") != expected_sub
    ):
        raise OAuthError("gmail_identity_mismatch")


async def authorize_runtime(credentials, client):
    """Refresh narrow user OAuth and verify selected identity; no ADC or mailbox read."""
    try:
        async with asyncio.timeout(10):
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "refresh_token": credentials["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=4,
            )
            response.raise_for_status()
            token = response.json()
            if (
                normalized_scopes(token.get("scope")) != SCOPES
                or token.get("token_type", "").lower() != "bearer"
                or not isinstance(token.get("access_token"), str)
                or not token["access_token"]
            ):
                raise OAuthError("gmail_scope_invalid")
            headers = {"Authorization": "Bearer " + token["access_token"]}
            identity = await client.get(USERINFO_URL, headers=headers, timeout=4)
            identity.raise_for_status()
            verify_identity(identity.json(), credentials["account_sub"])
            return headers
    except Exception:
        raise OAuthError("gmail_authorization_failed") from None
