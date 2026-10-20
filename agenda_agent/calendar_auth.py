"""Perfil OAuth independiente; nunca acepta el grant de envío de Gmail."""

from email_agent import gmail_auth

ACCOUNT = gmail_auth.SENDER
SCOPES = {"openid", "email", "https://www.googleapis.com/auth/calendar.events.owned"}
OAuthError = gmail_auth.OAuthError


def validate_credentials(data):
    gmail_auth.validate_credentials(data, expected_scopes=SCOPES)


def load_credentials(path):
    try:
        return gmail_auth.load_credentials(path, expected_scopes=SCOPES)
    except OAuthError:
        raise OAuthError(
            "Falta el grant independiente de Calendar o no pertenece a la cuenta de clases."
        ) from None


async def authorize_runtime(credentials, client):
    return await gmail_auth.authorize_runtime(credentials, client, expected_scopes=SCOPES)
