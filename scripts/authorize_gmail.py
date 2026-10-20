"""Autorice sender@example.com mediante OAuth de escritorio. No lee ni envía correos."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from google.oauth2 import id_token

from email_agent.gmail_auth import (
    PROJECT,
    SCOPES,
    SENDER,
    TOKEN_URL,
    USERINFO_URL,
    OAuthError,
    normalized_scopes,
    require_live_identity,
    validate_credentials,
    verify_identity,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CERT_URL = "https://www.googleapis.com/oauth2/v1/certs"
CONSENT_WINDOW_SECONDS = 1800


def load_client(path):
    try:
        target = Path(path).resolve()
        if target.stat().st_size > 16384:
            raise OAuthError("desktop_client_invalid")
        data = json.loads(target.read_text(encoding="utf-8"))["installed"]
        if (
            data.get("project_id") != PROJECT
            or data.get("auth_uri") not in {AUTH_URL, "https://accounts.google.com/o/oauth2/auth"}
            or data.get("token_uri") != TOKEN_URL
            or not isinstance(data.get("client_id"), str)
            or not data["client_id"].endswith(".apps.googleusercontent.com")
            or not isinstance(data.get("client_secret"), str)
            or not data["client_secret"]
        ):
            raise OAuthError("desktop_client_invalid")
        return data
    except Exception:
        raise OAuthError(
            "Se exige un cliente OAuth de escritorio del proyecto configurado"
        ) from None


class OAuthFlow:
    def __init__(self, config, redirect_uri, clock=None, *, scopes=None):
        redirect = urlsplit(redirect_uri)
        if (
            redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or not redirect.port
            or redirect.path != "/callback"
            or redirect.query
            or redirect.fragment
        ):
            raise OAuthError("loopback_redirect_required")
        self.config = config
        self.scopes = SCOPES if scopes is None else scopes
        self.redirect_uri = redirect_uri
        self.clock = clock or time.monotonic
        self.deadline = self.clock() + CONSENT_WINDOW_SECONDS
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.used = False
        self.exchanged = False
        self.code_digest = ""

    def authorization_url(self):
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return (
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": self.config["client_id"],
                    "redirect_uri": self.redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(sorted(self.scopes)),
                    "state": self.state,
                    "nonce": self.nonce,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "access_type": "offline",
                    "prompt": "consent select_account",
                    "login_hint": SENDER,
                }
            )
        )

    def callback(self, query):
        if self.used or self.clock() > self.deadline:
            raise OAuthError("flow_expired_or_used")
        self.used = True
        if len(query) > 8192:
            raise OAuthError("callback_invalid")
        values = parse_qs(query, keep_blank_values=True)
        if len(values.get("state", [])) != 1 or not hmac.compare_digest(
            values["state"][0], self.state
        ):
            raise OAuthError("state_mismatch")
        if "error" in values:
            raise OAuthError("oauth_denied")
        if len(values.get("code", [])) != 1 or not values["code"][0]:
            raise OAuthError("callback_invalid")
        code = values["code"][0]
        self.code_digest = hashlib.sha256(code.encode()).hexdigest()
        return code

    def accept_claims(self, claims, userinfo):
        if (
            claims.get("nonce") != self.nonce
            or claims.get("aud") != self.config["client_id"]
            or claims.get("iss") not in {"https://accounts.google.com", "accounts.google.com"}
            or not isinstance(claims.get("sub"), str)
        ):
            raise OAuthError("oidc_claim_mismatch")
        verify_identity(claims, claims["sub"])
        verify_identity(userinfo, claims["sub"])
        return claims["sub"]

    def exchange(self, code, client):
        if (
            not self.used
            or self.exchanged
            or self.clock() > self.deadline
            or hashlib.sha256(code.encode()).hexdigest() != self.code_digest
        ):
            raise OAuthError("flow_expired_or_used")
        self.exchanged = True
        try:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": self.config["client_id"],
                    "client_secret": self.config["client_secret"],
                    "code": code,
                    "code_verifier": self.verifier,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=4,
            )
            response.raise_for_status()
            tokens = response.json()
            if (
                normalized_scopes(tokens.get("scope")) != self.scopes
                or tokens.get("token_type", "").lower() != "bearer"
            ):
                raise OAuthError("gmail_scope_invalid")

            def certificate_request(url, method="GET", body=None, headers=None, **kwargs):
                if url != CERT_URL or method != "GET":
                    raise OAuthError("certificate_endpoint_invalid")
                certificate = client.get(CERT_URL, timeout=4)
                certificate.raise_for_status()
                return SimpleNamespace(status=certificate.status_code, data=certificate.content)

            # Actual pinned google-auth verifies signature, expiry, audience and issuer.
            claims = id_token.verify_oauth2_token(
                tokens["id_token"], certificate_request, audience=self.config["client_id"]
            )
            response = client.get(
                USERINFO_URL,
                headers={"Authorization": "Bearer " + tokens["access_token"]},
                timeout=4,
            )
            response.raise_for_status()
            sub = self.accept_claims(claims, response.json())
            values = {
                "type": "authorized_user",
                "project_id": PROJECT,
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "refresh_token": tokens["refresh_token"],
                "token_uri": TOKEN_URL,
                "account_email": SENDER,
                "account_sub": sub,
                "scopes": sorted(self.scopes),
            }
            validate_credentials(values, expected_scopes=self.scopes)
            return values
        except Exception:
            raise OAuthError(
                "Falló la verificación de la autorización OAuth; no se guardaron credenciales"
            ) from None


def _output_target(path):
    target = Path(path).resolve()
    project = Path(__file__).resolve().parents[1]
    if target.is_relative_to(project) or target.exists() or not target.parent.is_dir():
        raise OAuthError(
            "La salida debe ser un archivo nuevo en un directorio protegido existente "
            "fuera del proyecto"
        )
    return target


def _private_write(path, data):
    target = _output_target(path)
    fd = None
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if os.name == "nt":
            identity = subprocess.run(
                ["whoami"], capture_output=True, text=True, check=True, timeout=5
            ).stdout.strip()
            subprocess.run(
                ["icacls", str(target), "/inheritance:r", "/grant:r", f"{identity}:(R,W)"],
                capture_output=True,
                check=True,
                timeout=5,
            )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        raise OAuthError(
            "No se pudo escribir la salida protegida; no se imprime ningún secreto"
        ) from None
    finally:
        if fd is not None:
            os.close(fd)


def save_credentials(path, values, *, expected_scopes=None):
    validate_credentials(values, expected_scopes=expected_scopes)
    _private_write(path, json.dumps(values, indent=2) + "\n")


def receive_callback(config, browser=webbrowser.open, *, scopes=None):
    result = {}

    class LoopbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address

        def handle_error(self, request, client_address):
            pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log callback queries, codes or state.

        def do_GET(self):
            target = urlsplit(self.path)
            if (
                target.path != "/callback"
                or self.headers.get("Host") != urlsplit(flow.redirect_uri).netloc
            ):
                self.send_error(400)
                return
            try:
                result["code"] = flow.callback(target.query)
                status = 200
            except OAuthError:
                result["error"] = True
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                "Respuesta recibida. Cierre esta pestaña y vuelva a la terminal.".encode()
            )

    with LoopbackServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        flow = (
            OAuthFlow(config, redirect)
            if scopes is None
            else OAuthFlow(config, redirect, scopes=scopes)
        )
        if not browser(flow.authorization_url()):
            raise OAuthError(
                "No se pudo abrir el navegador para obtener el consentimiento explícito"
            )
        while not result and flow.clock() <= flow.deadline:
            server.handle_request()
        if "code" not in result:
            raise OAuthError(
                "La respuesta OAuth fue denegada, no es válida o superó el tiempo máximo"
            )
        return flow, result["code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client", required=True, help="Archivo JSON explícito del cliente OAuth de escritorio"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Archivo nuevo y protegido de credenciales fuera del código fuente",
    )
    parser.add_argument(
        "--signing-key-output",
        help="Archivo nuevo opcional y protegido de clave de firma persistente",
    )
    args = parser.parse_args()
    try:
        require_live_identity()
        _output_target(args.output)
        if args.signing_key_output:
            _output_target(args.signing_key_output)
            if Path(args.output).resolve() == Path(args.signing_key_output).resolve():
                raise OAuthError(
                    "Las credenciales y la clave de firma deben guardarse en archivos separados"
                )
        config = load_client(args.client)
        print(
            "El navegador solicitará permisos limitados de envío de Gmail y verificación de "
            f"identidad para {SENDER}. No se lee, redacta ni envía ningún correo."
        )
        flow, code = receive_callback(config)
        with httpx.Client(timeout=4, follow_redirects=False) as client:
            values = flow.exchange(code, client)
        save_credentials(args.output, values)
        if args.signing_key_output:
            _private_write(args.signing_key_output, secrets.token_urlsafe(48))
        print(
            "Credenciales verificadas y guardadas de forma segura. No se envió correo "
            "ni se modificaron las ADC de Vertex."
        )
    except OAuthError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
