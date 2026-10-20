"""Owner-only web boundary. Structured drafting and explicit sending are separate."""

import hashlib
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictBool

from demo_ui.agents import AgentClient
from demo_ui.auth import GoogleOIDC
from demo_ui.config import Config
from demo_ui.store import FirestoreStore, digest

STATIC = Path(__file__).parent / "static"
SESSION_SECONDS = 3600


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: EmailStr = Field(max_length=254)
    request: str = Field(min_length=1, max_length=4000)


class TripInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(pattern=r"^(Bogotá|Medellín|Cartagena)$")
    days: int = Field(ge=1, le=7, strict=True)
    interests: list[str] = Field(default_factory=list, max_length=2)
    prepare_email: StrictBool = False
    recipient: EmailStr | None = Field(None, max_length=254)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def identifier():
    return str(uuid4())


def public_operation(operation, row):
    result = {key: operation[key] for key in ("id", "kind", "state", "created")}
    result["message"] = {
        "pending": "La solicitud está en curso. No repita el envío.",
        "unknown": "El resultado aún no está confirmado. Compruebe el estado sin volver a enviar.",
        "completed": "Solicitud completada.",
        "error": "No se completó la solicitud. Revise los datos o prepare un nuevo borrador.",
    }[operation["state"]]
    raw = operation.get("result") or {}
    itinerary = raw.get("itinerary")
    if isinstance(itinerary, dict):
        result["itinerary"] = {
            "city": itinerary.get("city"),
            "summary": itinerary.get("summary"),
            "days": itinerary.get("days", []),
            "warnings": itinerary.get("warnings", []),
        }
    draft = row.get("drafts", {}).get(operation.get("draft_id"))
    if draft:
        preview = draft["email"]["preview"]
        result["draft"] = {
            "id": draft["id"],
            **{
                name: preview[name]
                for name in ("recipient", "subject", "text", "sender", "expires_at")
            },
            "send_operation": draft.get("send_operation"),
        }
    if operation["kind"] == "send" and operation["state"] in {"completed", "error"}:
        result["receipt"] = {
            "status": raw.get("status", "error"),
            "delivery": raw.get("delivery"),
        }
    return result


def saved_email(raw, expected_sender):
    email = raw.get("email") if "itinerary" in raw else raw
    if not isinstance(email, dict) or email.get("status") != "preview":
        return None
    preview = email.get("preview")
    if not isinstance(preview, dict) or not email.get("approval_token"):
        return None
    if not all(email.get(field) for field in ("request_id", "session_id", "plan_version")):
        raise ValueError("Missing email correlation")
    required = ("recipient", "subject", "text", "sender", "expires_at", "session_id")
    if not all(preview.get(field) for field in required):
        raise ValueError("Incomplete signed preview")
    if preview["session_id"] != email["session_id"]:
        raise ValueError("Email session mismatch")
    if preview.get("transport") != "gmail" or preview["sender"] != expected_sender:
        raise ValueError("Unexpected mail transport")
    return {
        key: email[key]
        for key in ("request_id", "session_id", "plan_version", "preview", "approval_token")
    }


def create_app(config=None, *, store=None, oidc=None, agents=None):
    config = config or Config.from_env()
    provided_store = store

    @asynccontextmanager
    async def lifespan(app):
        yield
        if provided_store is None and app.state.store is not None:
            await app.state.store.close()

    app = FastAPI(
        title="Colombia", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = store
    oidc = oidc or GoogleOIDC(config)
    agents = agents or AgentClient(config)

    def persistence():
        if not config.ready:
            raise HTTPException(503, "El acceso todavía no está configurado.")
        if app.state.store is None:
            app.state.store = FirestoreStore(config.project, config.database)
        return app.state.store

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": "Revise los campos y sus límites."}, status_code=422)

    @app.exception_handler(Exception)
    async def unavailable(request, error):
        return JSONResponse(
            {"detail": "Servicio no disponible. No repita un envío pendiente."}, status_code=503
        )

    @app.middleware("http")
    async def boundaries(request, call_next):
        response = None
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16_000:
                    response = JSONResponse(
                        {"detail": "La solicitud es demasiado extensa."}, status_code=413
                    )
                    break
            request._body = bytes(body)
        if response is None:
            try:
                if request.method == "POST" and (
                    request.url.path.startswith("/api/") or request.url.path == "/auth/logout"
                ):
                    await identity(request, True)
                response = await call_next(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                "style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        if config.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    async def identity(request, mutation=False):
        database = persistence()
        token = request.cookies.get(config.session_cookie, "")
        if not 43 <= len(token) <= 100:
            raise HTTPException(401, "Inicie sesión para continuar.")
        record = await database.atomic("session:" + digest(token), lambda row: dict(row))
        if not token or record.get("expires", 0) <= time.time():
            raise HTTPException(401, "Inicie sesión para continuar.")
        if mutation and (
            request.headers.get("origin") != config.origin
            or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), record["csrf"])
            or request.headers.get("content-type", "").split(";")[0] != "application/json"
        ):
            raise HTTPException(403, "La solicitud no pertenece a esta sesión.")
        return "user:" + digest(record["sub"]), record

    def cookie(response, name, value, seconds):
        response.set_cookie(
            name,
            value,
            max_age=seconds,
            path="/",
            secure=config.secure,
            httponly=True,
            samesite="lax",
        )

    @app.get("/health")
    async def health():
        return JSONResponse({"ready": config.ready}, status_code=200 if config.ready else 503)

    @app.get("/api/config")
    async def public_config():
        return {"configured": config.ready, "hub_return": config.hub_return if config.ready else ""}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    async def asset(name: str):
        if name not in {"app.js", "styles.css"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/auth/login")
    async def login():
        state, nonce, verifier, binding = (secrets.token_urlsafe(32) for _ in range(4))
        pending = {
            "nonce": nonce,
            "verifier": verifier,
            "binding": digest(binding),
            "expires": time.time() + 600,
            "delete_after": datetime.fromtimestamp(time.time() + 600, UTC),
        }
        await persistence().atomic("login:" + digest(state), lambda row: row.update(pending))
        import base64

        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = urlencode(
            {
                "client_id": config.client_id,
                "redirect_uri": config.origin + "/auth/callback",
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge.decode().rstrip("="),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        response = RedirectResponse(
            "https://accounts.google.com/o/oauth2/v2/auth?" + query, status_code=303
        )
        cookie(response, config.login_cookie, binding, 600)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request, state: str = "", code: str = ""):
        if not 20 <= len(state) <= 100 or not code or len(code) > 4000:
            raise HTTPException(400, "La autorización no es válida. Inicie sesión de nuevo.")
        binding = request.cookies.get(config.login_cookie, "")

        def consume(row):
            if (
                row.get("expires", 0) <= time.time()
                or not binding
                or not hmac.compare_digest(row.get("binding", ""), digest(binding))
            ):
                return None
            pending = dict(row)
            row.clear()
            return pending

        pending = await persistence().atomic("login:" + digest(state), consume)
        if not pending:
            raise HTTPException(
                400, "La autorización venció o ya se utilizó. Inicie sesión de nuevo."
            )
        try:
            claims = await oidc.exchange(code, pending["verifier"], pending["nonce"])
            if (
                claims.get("nonce") != pending["nonce"]
                or claims.get("email_verified") is not True
                or str(claims.get("email", "")).casefold() != config.owner_email
                or not isinstance(claims.get("sub"), str)
                or not 1 <= len(claims["sub"]) <= 255
            ):
                raise ValueError("Identity not allowed")
        except Exception:
            raise HTTPException(
                403, "Esta cuenta no tiene acceso o la autorización no es válida."
            ) from None
        old = request.cookies.get(config.session_cookie)
        if old:
            await persistence().atomic("session:" + digest(old), lambda row: row.clear())
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await persistence().atomic(
            "session:" + digest(token),
            lambda row: row.update(
                {
                    "sub": claims["sub"],
                    "email": config.owner_email,
                    "csrf": csrf,
                    "expires": time.time() + SESSION_SECONDS,
                    "delete_after": datetime.fromtimestamp(time.time() + SESSION_SECONDS, UTC),
                }
            ),
        )
        response = RedirectResponse("/", status_code=303)
        cookie(response, config.session_cookie, token, SESSION_SECONDS)
        response.delete_cookie(
            config.login_cookie, path="/", secure=config.secure, httponly=True, samesite="lax"
        )
        return response

    @app.get("/api/session")
    async def session(request: Request):
        user, record = await identity(request)
        row = await persistence().atomic(user, lambda row: dict(row))
        return {
            "email": record["email"],
            "csrf": record["csrf"],
            "operations": [
                public_operation(op, row)
                for op in sorted(row.get("operations", {}).values(), key=lambda op: op["created"])
            ],
        }

    @app.post("/auth/logout")
    async def logout(request: Request):
        await identity(request, True)
        await persistence().atomic(
            "session:" + digest(request.cookies[config.session_cookie]), lambda row: row.clear()
        )
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            config.session_cookie, path="/", secure=config.secure, httponly=True, samesite="lax"
        )
        return response

    def initialize(row):
        row.setdefault("operations", {})
        row.setdefault("drafts", {})
        now = time.time()
        row["recent"] = [stamp for stamp in row.get("recent", []) if now - stamp < 60]
        if len(row["recent"]) >= 10:
            raise HTTPException(429, "Espere un minuto antes de realizar otra solicitud.")
        if row.get("lock"):
            raise HTTPException(409, "Hay una operación pendiente. Compruebe su estado primero.")
        row["recent"].append(now)
        # A bounded workspace, not an unbounded history service. Never evict pending effects.
        while len(row["operations"]) >= 6:
            oldest = min(row["operations"], key=lambda key: row["operations"][key]["created"])
            removed = row["operations"].pop(oldest)
            row["drafts"].pop(removed.get("draft_id"), None)

    async def complete(user, operation_id, result=None, *, unknown=False):
        def finish(row):
            operation = row["operations"][operation_id]
            if unknown:
                operation["state"] = "unknown"
            else:
                operation["result"] = result
                accepted = (
                    result.get("status") == "sent" and result.get("delivery") == "provider_accepted"
                )
                rejected = result.get("status") == "error" and result.get("delivery") in {
                    "not_attempted",
                    "rejected",
                }
                effect_unknown = operation["kind"] == "send" and not (accepted or rejected)
                operation["state"] = (
                    "unknown"
                    if effect_unknown
                    else (
                        "error"
                        if result.get("status") in {"error", "clarification"}
                        else "completed"
                    )
                )
                if operation["kind"] != "send":
                    email = saved_email(result, config.sender_email)
                    if email:
                        draft_id = identifier()
                        row["drafts"][draft_id] = {"id": draft_id, "email": email}
                        operation["draft_id"] = draft_id
            if operation["state"] in {"completed", "error"} and row.get("lock") == operation_id:
                row.pop("lock", None)
            return public_operation(operation, row)

        return await persistence().atomic(user, finish)

    async def dispatch(user, operation, payload):
        try:
            result = await agents.post(operation["agent"], payload)
            if len(str(result)) > 65_000:
                raise ValueError("Workspace result exceeds limit")
            return await complete(user, operation["id"], result)
        except Exception:
            # A failed storage commit after a provider response is also uncertain for sends.
            return await complete(
                user, operation["id"], {"status": "error"}, unknown=operation["kind"] == "send"
            )

    async def generate(user, kind, payload):
        operation = {
            "id": identifier(),
            "kind": kind,
            "state": "pending",
            "created": time.time(),
            "agent": "itinerary" if kind == "trip" else "email",
        }
        payload.update(request_id=identifier(), session_id=identifier(), operation_id=identifier())
        operation["payload"] = payload

        def reserve(row):
            initialize(row)
            row["operations"][operation["id"]] = operation
            row["lock"] = operation["id"]

        await persistence().atomic(user, reserve)
        return await dispatch(user, operation, payload)

    @app.post("/api/itineraries")
    async def itinerary(request: Request, body: TripInput):
        user, _ = await identity(request, True)
        if any(value not in {"culture", "nature"} for value in body.interests):
            raise HTTPException(422, "Seleccione los intereses disponibles.")
        if body.prepare_email and not body.recipient:
            raise HTTPException(422, "Indique un destinatario para preparar el correo.")
        context = {
            "city": body.city,
            "days": body.days,
            "interests": body.interests,
            "draft_email": body.prepare_email,
            "agenda_requested": False,
        }
        payload = {"message": "", "context": context, "draft_email": body.prepare_email}
        if body.prepare_email:
            payload["recipient"] = str(body.recipient)
            context["recipient"] = str(body.recipient)
        return await generate(user, "trip", payload)

    @app.post("/api/drafts")
    async def draft(request: Request, body: DraftInput):
        user, _ = await identity(request, True)
        if not body.request.strip():
            raise HTTPException(422, "Indique qué desea redactar.")
        return await generate(
            user,
            "draft",
            {"action": "draft", "request": body.request, "recipient": str(body.recipient)},
        )

    @app.post("/api/drafts/{draft_id}/send")
    async def send(request: Request, draft_id: str, body: EmptyInput):
        user, _ = await identity(request, True)

        def reserve(row):
            draft = row.get("drafts", {}).get(draft_id)
            if not draft:
                raise HTTPException(404, "No se encontró el borrador en esta cuenta.")
            if draft.get("send_operation"):
                return (
                    False,
                    row["operations"][draft["send_operation"]],
                    public_operation(row["operations"][draft["send_operation"]], row),
                )
            if draft["email"]["preview"]["expires_at"] <= time.time():
                raise HTTPException(409, "El borrador venció. Prepare uno nuevo antes de enviarlo.")
            initialize(row)
            operation = {
                "id": identifier(),
                "kind": "send",
                "agent": "email",
                "state": "pending",
                "created": time.time(),
                "draft_id": draft_id,
                "payload": {
                    **draft["email"],
                    "action": "send",
                    "confirmed": True,
                    "operation_id": identifier(),
                },
            }
            # initialize may evict the originating history entry. Restore this reserved draft.
            row["drafts"][draft_id] = draft
            draft["send_operation"] = operation["id"]
            row["operations"][operation["id"]] = operation
            row["lock"] = operation["id"]
            return True, operation, None

        is_new, operation, existing = await persistence().atomic(user, reserve)
        return await dispatch(user, operation, operation["payload"]) if is_new else existing

    @app.get("/api/operations/{operation_id}")
    async def operation_status(request: Request, operation_id: str):
        user, _ = await identity(request)
        row = await persistence().atomic(user, lambda row: dict(row))
        operation = row.get("operations", {}).get(operation_id)
        if not operation:
            raise HTTPException(404, "No se encontró la operación en esta cuenta.")
        if operation["state"] in {"pending", "unknown"} and operation["kind"] == "send":
            payload = operation["payload"]
            try:
                snapshot = await agents.lookup(
                    "email", payload["request_id"], payload["session_id"]
                )
                match = next(
                    (
                        entry
                        for entry in snapshot.get("operations", [])
                        if entry.get("operation_id") == payload["operation_id"]
                    ),
                    None,
                )
                receipt = (match or {}).get("receipt")
                if (
                    snapshot.get("state") in {"completed", "failed", "cancelled"}
                    and (match or {}).get("state") in {"completed", "failed", "cancelled"}
                    and receipt
                    and receipt.get("status") in {"sent", "error"}
                ):
                    return await complete(user, operation_id, receipt)
            except Exception:
                pass
            return await complete(user, operation_id, unknown=True)
        return public_operation(operation, row)

    return app
