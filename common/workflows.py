"""Estado durable por servicio; una reserva incierta nunca habilita repetir efectos."""

import hashlib
import inspect
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validation_evidence(errors, fields):
    allowed_types = {
        "missing",
        "extra_forbidden",
        "int_type",
        "int_parsing",
        "greater_than_equal",
        "less_than_equal",
        "string_type",
        "string_too_short",
        "string_too_long",
        "string_pattern_mismatch",
        "list_type",
        "too_long",
        "too_short",
        "literal_error",
        "value_error",
        "model_type",
        "model_attributes_type",
    }
    return [
        {
            "loc": [
                part if isinstance(part, int) and 0 <= part < 100 or part in fields else "<extra>"
                for part in item["loc"][:6]
            ],
            "type": item["type"] if item["type"] in allowed_types else "value_error",
        }
        for item in errors[:8]
    ]


def child_operation(parent, phase):
    return str(uuid5(NAMESPACE_URL, f"{parent}:{phase}")) if parent else None


def semantic(value):
    ignored = {
        "request_id",
        "session_id",
        "operation_id",
        "plan_version",
        "approval_token",
        "expires_at",
        "draft_id",
        "plan_id",
        "retrieved_at",
        "source_reviewed_at",
        "email_context",
        "agenda_context",
        "status",
        "delivery",
        "error_code",
        "a2a_task_id",
        "a2a_context_id",
    }
    if isinstance(value, dict):
        return {
            key: semantic(item)
            for key, item in value.items()
            if key not in ignored and item is not None
        }
    if isinstance(value, list):
        return [semantic(item) for item in value]
    return value


def confirmation(request):
    # Las confirmaciones del contrato tipado no se infieren del mensaje natural.
    return getattr(request, "action", None) == "send"


def workflow_state(response):
    if response.get("status") == "cancelled":
        return "cancelled"
    nested = response.get("agenda") or {}
    if (
        response.get("error_code") == "calendar_not_configured"
        or nested.get("error_code") == "calendar_not_configured"
    ):
        return "blocked"
    if (
        response.get("status") in {"delivery_unknown", "creation_unknown"}
        or (response.get("email") or {}).get("status") == "delivery_unknown"
    ):
        return "unknown"
    for name in ("agenda", "email"):
        child = response.get(name) or {}
        if (
            child.get("draft")
            and child.get("conversation_state") == "draft"
            or child.get("status")
            in {
                "preview",
                "clarification",
                "creation_unknown",
                "delivery_unknown",
            }
        ):
            return workflow_state(child)
    if response.get("draft") and response.get("conversation_state") == "draft":
        return "draft"
    return {
        "preview": "awaiting_approval",
        "clarification": "awaiting_input",
        "error": "failed",
        "conflict": "blocked",
        "partial": "partial",
    }.get(response.get("status"), "completed")


def approval_of(response):
    owner = next(
        (
            item
            for item in (response, response.get("agenda") or {}, response.get("email") or {})
            if item.get("preview")
        ),
        {},
    )
    preview = owner.get("preview")
    if not preview:
        return None
    return {
        "digest": hashlib.sha256(encoded(semantic(preview)).encode()).hexdigest(),
        "expires_at": preview.get("expires_at"),
        "content": preview,
        "token": owner.get("approval_token"),
    }


def payload_state(payload):
    clean = semantic(
        {
            key: value
            for key, value in payload.items()
            if key != "context" or isinstance(value, dict)
        }
    )
    return clean, hashlib.sha256(encoded(clean).encode()).hexdigest()


def decoded_record(row, fields):
    if row is None:
        return None
    result = dict(row)
    for name in fields:
        result[name] = json.loads(result[name]) if result.get(name) else None
    return result


def begin_decision(
    request_id,
    session_id,
    payload,
    digest,
    row,
    prior,
    pending,
    *,
    expected_version=None,
    operation_id=None,
    confirmation=False,
    context=None,
    now,
    preserve_plan=False,
):
    """Política pura compartida: las reservas y sus recibos no caducan por edad."""
    previous = (row or {}).get("response") or {}
    version = row["version"] if row else expected_version or 1
    base = {
        "request_id": request_id,
        "session_id": session_id,
        "version": version,
        "context": previous.get("context"),
        "confirmation": confirmation,
        "previous_status": previous.get("status"),
        "preserve_plan": preserve_plan,
    }
    if row and row["session"] != session_id:
        return {**base, "action": "session_mismatch"}
    upstream = bool(
        operation_id
        and not confirmation
        and expected_version is not None
        and expected_version > version
    )
    if upstream and row and row["state"] == "unknown":
        return {**base, "action": "unknown"}
    if upstream and (payload.get("preview") or payload.get("approval_token")):
        return {**base, "action": "approval_mismatch"}
    if row and expected_version is not None and expected_version != version and not upstream:
        return {**base, "action": "stale"}
    if row and context is not None and context != previous.get("context"):
        return {**base, "action": "stale"}
    approval = (row or {}).get("approval")
    if confirmation and not row:
        return {**base, "action": "approval_mismatch"}
    if confirmation and (payload.get("preview") or payload.get("approval_token")):
        if (
            not approval
            or approval.get("version") != version
            or payload.get("preview") != approval.get("content")
            or payload.get("approval_token") != approval.get("token")
        ):
            return {**base, "action": "approval_mismatch"}
    if (
        confirmation
        and approval
        and (
            approval.get("version") != version
            or (
                approval["content"].get("plan_version")
                or approval["content"].get("version")
                or version
            )
            != version
        )
    ):
        return {**base, "action": "approval_mismatch"}
    if pending:
        return {**base, "operation_id": pending, "action": "unknown"}
    if row and confirmation and row["state"] in {"completed", "unknown", "partial"} and previous:
        return {**base, "action": "replay", "response": previous}
    if prior and not upstream:
        if prior["version"] != version:
            return {**base, "action": "stale"}
        if prior.get("receipt"):
            return {
                **base,
                "operation_id": prior["id"],
                "action": "replay",
                "response": prior["receipt"],
            }
    if confirmation and approval and (approval.get("expires_at") or 0) <= now:
        return {**base, "action": "expired"}
    if row and not confirmation and not preserve_plan:
        version = expected_version if upstream else version + 1
    return {
        **base,
        "version": version,
        "operation_id": operation_id or child_operation(request_id, f"{version}:{digest}"),
        "action": "execute",
        "revision": bool(row and not confirmation),
        "upstream_revision": upstream,
    }


def bounded_history(messages):
    messages = messages[-16:]
    while len(encoded(messages)) > 16000 and len(messages) > 2:
        messages = messages[2:]
    if len(encoded(messages)) > 16000:
        raise ValueError("El historial supera el límite durable de la conversación.")
    return messages


def finish_matches(row, operation, claim):
    if (
        not row
        or not operation
        or row["session"] != claim["session_id"]
        or row["version"] != claim["version"]
        or operation["request_id"] != claim["request_id"]
        or operation["version"] != claim["version"]
    ):
        raise ValueError("El recibo no corresponde a la reserva vigente.")
    return operation["state"] == "reserved"


def snapshot_record(owner, row, operations):
    response = row.get("response")
    tasks = [
        {
            "owner": owner,
            "state": "unknown" if row["state"] == "running" else row["state"],
            "reason": (response or {}).get("error_code"),
        }
    ]
    for name in ("agenda", "email"):
        child = (response or {}).get(name)
        if child:
            tasks.append(
                {
                    "owner": name,
                    "state": workflow_state(child),
                    "reason": child.get("error_code"),
                    "request_id": child.get("request_id"),
                    "operation_id": child.get("operation_id"),
                }
            )
    return {
        "request_id": row["id"],
        "session_id": row["session"],
        "version": row["version"],
        "state": "unknown" if row["state"] == "running" else row["state"],
        "response": response,
        "plan": row.get("plan"),
        "approval": row.get("approval"),
        "operations": [
            {
                "operation_id": op["id"],
                "version": op["version"],
                "owner": op["owner"],
                "state": "unknown" if op["state"] == "reserved" else op["state"],
                "receipt": op.get("receipt"),
            }
            for op in operations
        ],
        "tasks": tasks,
    }


class WorkflowStore:
    def __init__(self, path, owner):
        target = Path(path)
        if target.is_symlink():
            raise ValueError("El estado durable no admite enlaces simbólicos.")
        self.path = target.resolve()
        self.owner = owner
        if self.path.is_relative_to(Path(__file__).resolve().parents[1]) or self.path.is_symlink():
            raise ValueError(
                "El estado durable debe estar fuera del proyecto y ser un archivo regular."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, version INTEGER NOT NULL,
                    state TEXT NOT NULL, response TEXT, approval TEXT, plan TEXT,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, version INTEGER NOT NULL,
                    digest TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL,
                    input TEXT NOT NULL, receipt TEXT, started REAL NOT NULL, finished REAL,
                    UNIQUE(request_id,version,digest)
                );
                CREATE TABLE IF NOT EXISTS continuations (
                    digest TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    session TEXT NOT NULL, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_history (
                    request_id TEXT PRIMARY KEY, session TEXT NOT NULL, messages TEXT NOT NULL
                );
            """)
            if "plan" not in {row["name"] for row in db.execute("PRAGMA table_info(requests)")}:
                db.execute("ALTER TABLE requests ADD COLUMN plan TEXT")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record_tool_failure(self, request, evidence):
        """Añade diagnóstico saneado al audit privado; no modifica plan ni aprobación."""
        with self.connect() as db:
            row = db.execute(
                "SELECT o.input FROM operations o JOIN requests r ON r.id=o.request_id "
                "WHERE o.id=? AND o.request_id=? AND o.version=? AND r.session=? "
                "AND o.state='reserved'",
                (
                    request.operation_id,
                    request.request_id,
                    request.plan_version,
                    request.session_id,
                ),
            ).fetchone()
            if row:
                data = json.loads(row["input"])
                data["_tool_failures"] = [*data.get("_tool_failures", []), evidence][-6:]
                db.execute(
                    "UPDATE operations SET input=? WHERE id=?",
                    (encoded(data), request.operation_id),
                )

    def begin(
        self,
        request_id,
        session_id,
        payload,
        *,
        expected_version=None,
        operation_id=None,
        confirmation=False,
        context=None,
        now=None,
        preserve_plan=False,
    ):
        now = time.time() if now is None else now
        clean, digest = payload_state(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = decoded_record(
                db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone(),
                ("response", "approval", "plan"),
            )
            pending = db.execute(
                "SELECT id FROM operations WHERE request_id=? AND state='reserved'", (request_id,)
            ).fetchone()
            prior = decoded_record(
                db.execute(
                    "SELECT * FROM operations WHERE request_id=? AND digest=? "
                    "ORDER BY version DESC LIMIT 1",
                    (request_id, digest),
                ).fetchone(),
                ("receipt",),
            )
            claim = begin_decision(
                request_id,
                session_id,
                payload,
                digest,
                row,
                prior,
                pending["id"] if pending else None,
                expected_version=expected_version,
                operation_id=operation_id,
                confirmation=confirmation,
                context=context,
                now=now,
                preserve_plan=preserve_plan,
            )
            if claim["action"] != "execute":
                return claim
            version, operation_id = claim["version"], claim["operation_id"]
            if db.execute("SELECT id FROM operations WHERE id=?", (operation_id,)).fetchone():
                return {
                    **claim,
                    "version": row["version"] if row else claim["version"],
                    "action": "stale",
                }
            db.execute(
                (
                    "INSERT INTO requests(id,session,version,state,updated) VALUES "
                    "(?,?,?,'running',?) ON CONFLICT(id) DO UPDATE SET "
                    "version=excluded.version,state='running',approval=CASE WHEN ? "
                    "THEN approval ELSE NULL END,updated=excluded.updated"
                ),
                (request_id, session_id, version, now, confirmation or preserve_plan),
            )
            db.execute(
                (
                    "INSERT INTO "
                    "operations(id,request_id,version,digest,owner,state,input,st"
                    "arted) VALUES (?,?,?,?,?,'reserved',?,?)"
                ),
                (operation_id, request_id, version, digest, self.owner, encoded(clean), now),
            )
            return claim

    def finish(self, claim, response, *, now=None, history=None):
        now = time.time() if now is None else now
        approval = approval_of(response)
        if approval:
            approval["version"] = claim["version"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM requests WHERE id=?", (claim["request_id"],)
            ).fetchone()
            operation = db.execute(
                "SELECT * FROM operations WHERE id=?", (claim["operation_id"],)
            ).fetchone()
            if not finish_matches(current, operation, claim):
                return
            if claim.get("preserve_plan") or approval is None and claim.get("confirmation"):
                row = db.execute(
                    "SELECT approval FROM requests WHERE id=?", (claim["request_id"],)
                ).fetchone()
                approval = json.loads(row["approval"]) if row and row["approval"] else None
            if approval and not claim.get("preserve_plan"):
                approval["confirmed"] = bool(claim.get("confirmation"))
            for value in (response.get("context"), response.get("approval_token")):
                if value:
                    digest = hashlib.sha256(encoded(value).encode()).hexdigest()
                    db.execute(
                        (
                            "INSERT OR IGNORE INTO "
                            "continuations(digest,request_id,session,version) VALUES (?,?,?,?)"
                        ),
                        (digest, claim["request_id"], claim["session_id"], claim["version"]),
                    )
            plan = (
                response.get("itinerary")
                or response.get("preview")
                or response.get("pending_plan")
                or response.get("events")
                or (response.get("agenda") or {}).get("preview")
                or (response.get("email") or {}).get("preview")
            )
            db.execute(
                (
                    "UPDATE operations SET state=?,receipt=?,finished=? WHERE id=? "
                    "AND state='reserved'"
                ),
                (workflow_state(response), encoded(response), now, claim["operation_id"]),
            )
            db.execute(
                (
                    "UPDATE requests SET "
                    "state=?,response=?,approval=?,plan=COALESCE(?,plan),updated=? "
                    "WHERE id=? AND version=?"
                ),
                (
                    workflow_state(response),
                    encoded(response),
                    encoded(approval) if approval else None,
                    encoded(plan) if plan else None,
                    now,
                    claim["request_id"],
                    claim["version"],
                ),
            )
            if history is not None:
                row = db.execute(
                    "SELECT messages FROM conversation_history WHERE request_id=? AND session=?",
                    (claim["request_id"], claim["session_id"]),
                ).fetchone()
                messages = (json.loads(row["messages"]) if row else []) + history
                messages = bounded_history(messages)
                db.execute(
                    "INSERT INTO conversation_history(request_id,session,messages) VALUES (?,?,?) "
                    "ON CONFLICT(request_id) DO UPDATE SET messages=excluded.messages",
                    (claim["request_id"], claim["session_id"], encoded(messages)),
                )

    def session_matches(self, request_id, session_id):
        with self.connect() as db:
            row = db.execute("SELECT session FROM requests WHERE id=?", (request_id,)).fetchone()
            return row is None or row["session"] == session_id

    def history(self, request_id, session_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT h.messages FROM conversation_history h JOIN requests r "
                "ON r.id=h.request_id AND r.session=h.session WHERE r.id=? AND r.session=?",
                (request_id, session_id),
            ).fetchone()
            return json.loads(row["messages"]) if row else []

    def replay(self, request_id, session_id, payload):
        clean = semantic(
            {k: v for k, v in payload.items() if k != "context" or isinstance(v, dict)}
        )
        digest = hashlib.sha256(encoded(clean).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT o.receipt FROM operations o JOIN requests r ON r.id=o.request_id "
                "WHERE r.id=? AND r.session=? AND o.version=r.version AND o.digest=?",
                (request_id, session_id, digest),
            ).fetchone()
            return json.loads(row["receipt"]) if row and row["receipt"] else None

    def snapshot(self, request_id, session_id):
        with self.connect() as db:
            row = decoded_record(
                db.execute(
                    "SELECT * FROM requests WHERE id=? AND session=?", (request_id, session_id)
                ).fetchone(),
                ("response", "approval", "plan"),
            )
            if row is None:
                return None
            operations = [
                decoded_record(op, ("receipt",))
                for op in db.execute(
                    "SELECT * FROM operations WHERE request_id=? ORDER BY started,id", (request_id,)
                )
            ]
            return snapshot_record(self.owner, row, operations)

    def recent_inputs(self, request_id, session_id):
        with self.connect() as db:
            return [
                json.loads(row["input"])
                for row in db.execute(
                    "SELECT o.input FROM operations o JOIN requests r ON r.id=o.request_id "
                    "WHERE r.id=? AND r.session=? ORDER BY o.started DESC,o.id DESC",
                    (request_id, session_id),
                )
            ]

    def resolve(self, value):
        if not value:
            return None
        digest = hashlib.sha256(encoded(value).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT * FROM continuations WHERE digest=?", (digest,)).fetchone()
            return dict(row) if row else None


class WorkflowRunner:
    def __init__(self, path, role, response_type, clock=None, *, store=None):
        self.store = store if store is not None else WorkflowStore(path, role)
        self.role = role
        self.response_type = response_type
        self.clock = clock or time.time

    @classmethod
    def from_settings(cls, settings, role, response_type, clock=None):
        if settings.workflow_store == "firestore":
            from common.firestore_workflows import FirestoreWorkflowStore

            store = FirestoreWorkflowStore(
                settings.workflow_firestore_project, settings.workflow_firestore_database, role
            )
            return cls(None, role, response_type, clock, store=store)
        return (
            cls(settings.workflow_db_path, role, response_type, clock)
            if settings.workflow_db_path
            else None
        )

    async def call(self, name, *args, **kwargs):
        result = getattr(self.store, name)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def failure(self, request, claim):
        unknown = claim["action"] == "unknown"
        data = {
            "status": "delivery_unknown"
            if unknown and self.role == "email"
            else "creation_unknown"
            if unknown and self.role == "agenda"
            else "partial"
            if unknown
            else "error",
            "message": (
                "La operación quedó reservada o incierta; no se repitió ningún "
                "efecto. Consulte su estado."
            )
            if unknown
            else (
                "La solicitud pertenece a otra sesión, versión o aprobación "
                "vencida. No se ejecutó una operación nueva."
            ),
            "error_code": "workflow_" + claim["action"],
            "request_id": claim["request_id"],
            "session_id": claim["session_id"],
            "plan_version": claim["version"],
            "operation_id": claim.get("operation_id"),
            "workflow_state": "unknown" if unknown else "blocked",
        }
        if self.role == "tourism":
            data["context"] = request.context
        return self.response_type.model_validate(data)

    async def run(
        self,
        request,
        execute,
        *,
        preserve_plan=False,
        history=None,
        invalidate_approval=False,
        confirmed_action=False,
    ):
        payload = request.model_dump(mode="json")
        context = payload.get("context")
        if (
            context == {}
            or hasattr(request.context, "model_fields_set")
            and not request.context.model_fields_set
        ):
            context = None
        continuation = await self.call(
            "resolve", context or getattr(request, "approval_token", None)
        )
        request_id = request.request_id or (continuation or {}).get("request_id") or str(uuid4())
        session = request.session_id or (continuation or {}).get("session") or str(uuid4())
        expected_version = (
            request.plan_version
            or (continuation or {}).get("version")
            or getattr(getattr(request, "preview", None), "plan_version", None)
        )
        if request.request_id and continuation and request.request_id != continuation["request_id"]:
            return self.failure(
                request,
                {
                    "action": "stale",
                    "request_id": request_id,
                    "session_id": session,
                    "version": expected_version or 1,
                },
            )
        claim = await self.call(
            "begin",
            request_id,
            session,
            payload,
            expected_version=expected_version,
            operation_id=request.operation_id,
            confirmation=(confirmation(request) or confirmed_action) and not invalidate_approval,
            context=context,
            now=self.clock(),
            preserve_plan=preserve_plan and not invalidate_approval,
        )
        if claim["action"] == "replay":
            return self.response_type.model_validate(claim["response"])
        if claim["action"] != "execute":
            return self.failure(request, claim)
        updates = {
            "request_id": request_id,
            "session_id": session,
            "plan_version": claim["version"],
            "operation_id": claim["operation_id"],
        }
        if (
            context is None
            and claim["context"] is not None
            and not (self.role == "email" and claim.get("upstream_revision"))
        ):
            if self.role == "tourism":
                updates["context"] = type(request.context).model_validate(claim["context"])
            else:
                updates["context"] = claim["context"]
        if (
            self.role == "agenda"
            and claim.get("revision")
            and claim.get("previous_status") not in {"clarification", "conflict"}
        ):
            updates["context"] = None
        prepared = request.model_copy(update=updates)
        # Ninguna transacción de almacenamiento permanece abierta durante una llamada externa.
        response = await execute(prepared)
        data = response.model_dump(mode="json")
        data.update(updates)
        data["context"] = response.model_dump(mode="json").get("context")
        if self.role == "tourism" and data["context"] is not None:
            data["context"].update(request_id=request_id, plan_version=claim["version"])
        data["workflow_state"] = workflow_state(data)
        await self.call(
            "finish",
            claim,
            data,
            now=self.clock(),
            history=history(data) if history is not None else None,
        )
        return self.response_type.model_validate(data)
