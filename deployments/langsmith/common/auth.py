"""Clave por servicio; no representa identidad de usuario ni sustituye aprobaciones."""

import hmac

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader


class ServiceAuth:
    def __init__(self, app, key, required=True):
        self.app, self.key, self.required = app, key.encode(), required

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and self.required
            and (
                path.startswith("/api/") or (path.rstrip("/") == "/a2a" or path.startswith("/a2a/"))
            )
        ):
            values = [value for name, value in scope["headers"] if name.lower() == b"x-api-key"]
            if not self.key:
                return await JSONResponse(
                    {"detail": "La autenticación del servicio no está configurada."},
                    status_code=503,
                )(scope, receive, send)
            if len(values) != 1 or not hmac.compare_digest(values[0], self.key):
                return await JSONResponse(
                    {"detail": "Se exige una clave válida de este servicio."},
                    status_code=401,
                    headers={"WWW-Authenticate": "APIKey"},
                )(scope, receive, send)
        await self.app(scope, receive, send)


def configure_auth(app, settings, service):
    scheme = APIKeyHeader(
        name="X-API-Key",
        scheme_name="Clave del servicio",
        description=(
            "Clave privada de esta API. No es una autorización de envío ni una identidad personal."
        ),
        auto_error=False,
    )
    dependency = Depends(scheme)

    @app.get(
        "/api/requests/{request_id}",
        tags=["Estado durable"],
        summary="Consultar solicitud y operaciones",
    )
    async def request_state(request_id: str, session_id: str, key=dependency):
        state = (
            await service.workflow.call("snapshot", request_id, session_id)
            if service.workflow
            else None
        )
        if state is None:
            raise HTTPException(404, "No existe una solicitud de esa sesión en este servicio.")
        return state

    app.add_middleware(ServiceAuth, key=settings.service_api_key, required=settings.auth_required)
    original = app.openapi

    def openapi():
        schema = original()
        for path, methods in schema["paths"].items():
            if path.startswith("/api/") or path == "/a2a":
                for operation in methods.values():
                    if isinstance(operation, dict):
                        operation["security"] = [{"Clave del servicio": []}]
        return schema

    app.openapi = openapi
