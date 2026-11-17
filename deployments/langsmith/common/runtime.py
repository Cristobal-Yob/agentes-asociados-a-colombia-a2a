import asyncio
import json
import logging
import time

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class CapacityExceeded(TimeoutError):
    """No graph execution began because all request slots remained occupied."""


class BoundedGraph:
    def __init__(self):
        self.slots = asyncio.Semaphore(8)

    async def run(self, graph, state):
        acquired = False
        try:
            try:
                async with asyncio.timeout(0.3):
                    await self.slots.acquire()
                    acquired = True
            except TimeoutError:
                raise CapacityExceeded from None
            async with asyncio.timeout(35):
                return await graph.ainvoke(state, config={"recursion_limit": 12})
        finally:
            if acquired:
                self.slots.release()


class SafeBoundary:
    """ASGI size/deadline boundary; logs only event, status, elapsed time."""

    def __init__(self, app, max_bytes=32768):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        async with asyncio.timeout(5):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > self.max_bytes:
                    await send({"type": "http.response.start", "status": 413, "headers": []})
                    await send(
                        {"type": "http.response.body", "body": b"La solicitud es demasiado grande."}
                    )
                    return
                chunks.append(message)
                if not message.get("more_body"):
                    break
        start = time.perf_counter()
        status = 500

        async def replay():
            if chunks:
                return chunks.pop(0)
            return await receive()

        async def safe_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            async with asyncio.timeout(40):
                await self.app(scope, replay, safe_send)
        finally:
            logging.getLogger("classroom").info(
                json.dumps(
                    {
                        "event": "http_request",
                        "status": status,
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                    }
                )
            )


def base_app(name, description, health_state=None):
    app = FastAPI(title=name, description=description, version="0.1.0")
    app.add_middleware(SafeBoundary)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path not in {"/api/chat", "/api/email"}:
            return await request_validation_exception_handler(request, error)
        messages = {
            "missing": "El campo es obligatorio.",
            "extra_forbidden": "No se permiten campos adicionales.",
            "bool_type": "El valor debe ser un booleano válido.",
            "int_type": "El valor debe ser un número entero.",
            "int_parsing": "El valor no se puede interpretar como un número entero.",
            "string_type": "El valor debe ser texto.",
            "string_too_short": "El texto no alcanza la longitud mínima permitida.",
            "string_too_long": "El texto supera la longitud máxima permitida.",
            "string_pattern_mismatch": "El texto no cumple el formato permitido.",
            "greater_than_equal": "El valor es menor que el mínimo permitido.",
            "less_than_equal": "El valor supera el máximo permitido.",
            "too_long": "El valor supera la longitud máxima permitida.",
            "list_type": "El valor debe ser una lista.",
            "literal_error": "El valor no está entre los valores admitidos por el esquema.",
            "json_invalid": "El contenido no es JSON válido.",
            "model_attributes_type": "El contenido debe ser un objeto válido.",
            "value_error": "El valor no es válido; consulte el formato exigido por el esquema.",
        }
        detail = [
            {
                **item,
                "msg": messages.get(
                    item["type"], "El valor no es válido; consulte el esquema y el código de error."
                ),
            }
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": detail}))

    @app.get(
        "/health",
        summary="Consultar el estado del agente",
        description="Comprueba que la API responde. No consulta modelos ni proveedores externos.",
        tags=["Estado"],
        response_description="Estado del agente",
    )
    async def health():
        return {"status": "ok", "agent": name, **(health_state() if health_state else {})}

    return app
