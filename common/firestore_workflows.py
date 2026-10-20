"""Persistencia Firestore con reservas atómicas y efectos fuera de transacciones."""

import asyncio
import hashlib
import time

from google.cloud.firestore_v1 import AsyncClient, async_transactional
from google.cloud.firestore_v1.base_query import FieldFilter

from common.workflows import (
    approval_of,
    begin_decision,
    bounded_history,
    decoded_record,
    encoded,
    finish_matches,
    payload_state,
    snapshot_record,
    workflow_state,
)


def document_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def digest_key(request_id, digest):
    return document_key(encoded([request_id, digest]))


def checked_document(data):
    # Margen conservador para nombres de campos y metadatos del documento (límite: 1 MiB).
    if len(encoded(data).encode()) > 900_000:
        raise ValueError("El estado excede el tamaño seguro de un documento Firestore.")
    return data


class FirestoreWorkflowStore:
    def __init__(self, project, database, owner, *, client=None, transactional=None):
        if not project or not database or database == "(default)":
            raise ValueError("Firestore exige proyecto y base dedicada explícitos.")
        self.owner = owner
        self.client = (
            client if client is not None else AsyncClient(project=project, database=database)
        )
        self.transactional = transactional or async_transactional

    def reference(self, collection, identifier):
        return self.client.collection(collection).document(document_key(identifier))

    async def read(self, reference, transaction=None):
        snapshot = await reference.get(transaction=transaction, timeout=3, retry=None)
        return snapshot.to_dict() if snapshot.exists else None

    async def atomic(self, callback):
        async with asyncio.timeout(8):
            return await self.transactional(callback)(self.client.transaction(max_attempts=3))

    async def operations(self, request_id, transaction=None):
        query = self.client.collection("operations").where(
            filter=FieldFilter("request_id", "==", request_id)
        )
        rows = [
            item.to_dict()
            async for item in query.stream(transaction=transaction, timeout=3, retry=None)
        ]
        return sorted(rows, key=lambda item: (item["started"], item["id"]))

    async def begin(
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
        request_ref = self.reference("requests", request_id)
        index_ref = self.client.collection("digests").document(digest_key(request_id, digest))

        async def reserve(tx):
            row = await self.read(request_ref, tx)
            index = await self.read(index_ref, tx)
            prior = (
                await self.read(self.reference("operations", index["operation_id"]), tx)
                if index
                else None
            )
            claim = begin_decision(
                request_id,
                session_id,
                payload,
                digest,
                decoded_record(row, ("response", "approval", "plan")),
                decoded_record(prior, ("receipt",)),
                (row or {}).get("pending"),
                expected_version=expected_version,
                operation_id=operation_id,
                confirmation=confirmation,
                context=context,
                now=now,
                preserve_plan=preserve_plan,
            )
            if claim["action"] != "execute":
                return claim
            op_ref = self.reference("operations", claim["operation_id"])
            if await self.read(op_ref, tx):
                return {
                    **claim,
                    "version": row["version"] if row else claim["version"],
                    "action": "stale",
                }
            current = {
                **(row or {}),
                "id": request_id,
                "session": session_id,
                "version": claim["version"],
                "state": "running",
                "updated": now,
                "pending": claim["operation_id"],
            }
            if not confirmation and not preserve_plan:
                current["approval"] = None
            operation = {
                "id": claim["operation_id"],
                "request_id": request_id,
                "version": claim["version"],
                "digest": digest,
                "owner": self.owner,
                "state": "reserved",
                "input": encoded(clean),
                "receipt": None,
                "started": now,
                "finished": None,
            }
            tx.set(request_ref, checked_document(current))
            tx.set(op_ref, checked_document(operation))
            tx.set(index_ref, {"operation_id": claim["operation_id"]})
            return claim

        return await self.atomic(reserve)

    async def finish(self, claim, response, *, now=None, history=None):
        now = time.time() if now is None else now
        request_ref = self.reference("requests", claim["request_id"])
        op_ref = self.reference("operations", claim["operation_id"])

        async def complete(tx):
            row = await self.read(request_ref, tx)
            operation = await self.read(op_ref, tx)
            if not finish_matches(row, operation, claim):
                return
            links = []
            for value in (response.get("context"), response.get("approval_token")):
                if value:
                    digest = document_key(encoded(value))
                    ref = self.client.collection("continuations").document(digest)
                    existing = await self.read(ref, tx)
                    links.append((ref, digest, existing))
            approval = approval_of(response)
            if approval:
                approval["version"] = claim["version"]
            if claim.get("preserve_plan") or approval is None and claim.get("confirmation"):
                approval = decoded_record(row, ("approval",))["approval"]
            if approval and not claim.get("preserve_plan"):
                approval["confirmed"] = bool(claim.get("confirmation"))
            plan = (
                response.get("itinerary")
                or response.get("preview")
                or response.get("pending_plan")
                or response.get("events")
                or (response.get("agenda") or {}).get("preview")
                or (response.get("email") or {}).get("preview")
            )
            updated = {
                **row,
                "state": workflow_state(response),
                "response": encoded(response),
                "approval": encoded(approval) if approval else None,
                "plan": encoded(plan) if plan else row.get("plan"),
                "updated": now,
                "pending": None,
            }
            if history is not None:
                updated["history"] = bounded_history([*row.get("history", []), *history])
            operation = {
                **operation,
                "state": workflow_state(response),
                "receipt": encoded(response),
                "finished": now,
            }
            # Todas las lecturas preceden a las escrituras; el callback sólo calcula estado.
            tx.set(request_ref, checked_document(updated))
            tx.set(op_ref, checked_document(operation))
            for ref, digest, existing in links:
                if existing is None:
                    tx.set(
                        ref,
                        {
                            "digest": digest,
                            "request_id": claim["request_id"],
                            "session": claim["session_id"],
                            "version": claim["version"],
                        },
                    )

        await self.atomic(complete)

    async def session_matches(self, request_id, session_id):
        async with asyncio.timeout(8):
            row = await self.read(self.reference("requests", request_id))
        return row is None or row["session"] == session_id

    async def snapshot(self, request_id, session_id):
        async def collect(tx):
            row = await self.read(self.reference("requests", request_id), tx)
            if row is None or row["session"] != session_id:
                return None
            operations = await self.operations(request_id, tx)
            return snapshot_record(
                self.owner,
                decoded_record(row, ("response", "approval", "plan")),
                [decoded_record(op, ("receipt",)) for op in operations],
            )

        return await self.atomic(collect)

    async def history(self, request_id, session_id):
        async with asyncio.timeout(8):
            row = await self.read(self.reference("requests", request_id))
        return row.get("history", []) if row and row["session"] == session_id else []

    async def recent_inputs(self, request_id, session_id):
        async def collect(tx):
            row = await self.read(self.reference("requests", request_id), tx)
            if row is None or row["session"] != session_id:
                return []
            operations = await self.operations(request_id, tx)
            return [decoded_record(op, ("input",))["input"] for op in reversed(operations)]

        return await self.atomic(collect)

    async def resolve(self, value):
        if not value:
            return None
        async with asyncio.timeout(8):
            return await self.read(
                self.client.collection("continuations").document(document_key(encoded(value)))
            )

    async def replay(self, request_id, session_id, payload):
        _, digest = payload_state(payload)

        async def collect(tx):
            row = await self.read(self.reference("requests", request_id), tx)
            if row is None or row["session"] != session_id:
                return None
            index = await self.read(
                self.client.collection("digests").document(digest_key(request_id, digest)), tx
            )
            op = (
                await self.read(self.reference("operations", index["operation_id"]), tx)
                if index
                else None
            )
            return (
                decoded_record(op, ("receipt",))["receipt"]
                if op and op["version"] == row["version"]
                else None
            )

        return await self.atomic(collect)

    async def record_tool_failure(self, request, evidence):
        async def record(tx):
            row = await self.read(self.reference("requests", request.request_id), tx)
            ref = self.reference("operations", request.operation_id)
            op = await self.read(ref, tx)
            if (
                not row
                or not op
                or row["session"] != request.session_id
                or row["version"] != request.plan_version
                or op["request_id"] != request.request_id
                or op["version"] != request.plan_version
                or op["state"] != "reserved"
            ):
                return
            data = decoded_record(op, ("input",))["input"]
            data["_tool_failures"] = [*data.get("_tool_failures", []), evidence][-6:]
            tx.set(ref, checked_document({**op, "input": encoded(data)}))

        await self.atomic(record)


def sqlite_documents(requests, operations, continuations, histories):
    """Proyecta filas exportadas; no lee SQLite ni escribe Firestore o proveedores.

    El operador crea documentos ausentes y reconcilia resultados inciertos antes
    del cambio de backend. Los JSON, IDs, firmas y timestamps se conservan.
    """
    result = {}
    by_request = {}
    for source in requests:
        row = dict(source)
        row["pending"] = None
        row["history"] = []
        by_request[row["id"]] = row
    latest = {}
    for source in operations:
        row = dict(source)
        parent = by_request[row["request_id"]]
        if row["state"] == "reserved":
            if parent["pending"]:
                raise ValueError("Existen varias reservas para una misma solicitud.")
            parent["pending"] = row["id"]
        result["operations/" + document_key(row["id"])] = checked_document(row)
        key = digest_key(row["request_id"], row["digest"])
        if key not in latest or row["version"] > latest[key]["version"]:
            latest[key] = row
    for key, row in latest.items():
        result["digests/" + key] = {"operation_id": row["id"]}
    for source in histories:
        row = dict(source)
        parent = by_request[row["request_id"]]
        if row["session"] != parent["session"]:
            raise ValueError("El historial no corresponde a la sesión exportada.")
        messages = decoded_record(row, ("messages",))["messages"]
        if bounded_history(messages) != messages:
            raise ValueError("El historial exportado excede los límites vigentes.")
        parent["history"] = messages
    for identifier, row in by_request.items():
        result["requests/" + document_key(identifier)] = checked_document(row)
    for source in continuations:
        row = dict(source)
        parent = by_request[row["request_id"]]
        if row["session"] != parent["session"]:
            raise ValueError("La continuación no corresponde a la sesión exportada.")
        result["continuations/" + row["digest"]] = checked_document(row)
    return result
