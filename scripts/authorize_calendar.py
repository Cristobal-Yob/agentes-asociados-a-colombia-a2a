"""Autorice Calendar de sender@example.com sin leer ni modificar Gmail."""

import argparse
import secrets
import webbrowser
from pathlib import Path

import httpx

from agenda_agent.calendar_auth import SCOPES
from scripts import authorize_gmail

load_client = authorize_gmail.load_client
_private_write = authorize_gmail._private_write


class OAuthFlow(authorize_gmail.OAuthFlow):
    def __init__(self, config, redirect_uri, clock=None):
        super().__init__(config, redirect_uri, clock, scopes=SCOPES)


def receive_callback(config, browser=webbrowser.open):
    return authorize_gmail.receive_callback(config, browser, scopes=SCOPES)


def save_credentials(path, values):
    authorize_gmail.save_credentials(path, values, expected_scopes=SCOPES)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client", required=True, help="Cliente Desktop existente de example-project"
    )
    parser.add_argument("--output", required=True, help="Archivo nuevo privado fuera del proyecto")
    parser.add_argument(
        "--signing-key-output",
        help="Archivo nuevo opcional para la clave propia de Agenda, separado de Gmail",
    )
    args = parser.parse_args()
    from email_agent.gmail_auth import require_live_identity

    require_live_identity()
    try:
        authorize_gmail._output_target(args.output)
        if args.signing_key_output:
            authorize_gmail._output_target(args.signing_key_output)
            if Path(args.output).resolve() == Path(args.signing_key_output).resolve():
                raise authorize_gmail.OAuthError(
                    "El grant y la clave propia de Agenda requieren archivos separados"
                )
        config = load_client(args.client)
        print(
            "Revise y autorice los permisos de Calendar para la cuenta de "
            "clases. La espera máxima es de 30 minutos. No se crean eventos "
            "durante OAuth."
        )
        flow, code = receive_callback(config)
        with httpx.Client(timeout=4, follow_redirects=False) as client:
            values = flow.exchange(code, client)
        save_credentials(args.output, values)
        if args.signing_key_output:
            _private_write(args.signing_key_output, secrets.token_urlsafe(48))
        print("Autorización de Calendar verificada y guardada. No se creó ningún evento.")
    except authorize_gmail.OAuthError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
