"""Contrato durable con transacciones optimistas herméticas, sin servicio Firestore."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest


class Conflict(Exception):
    pass


class FakeClient:
    def __init__(self):
        self.docs, self.versions = {}, {}
        self.lock = asyncio.Lock()
        self.fail_commit = False
        self.retry_once = False
        self.attempts = 0
        self.in_transaction = False

    def collection(self, name):
        return Collection(self, name)

    def transaction(self, *, max_attempts):
        return Transaction(self, max_attempts)

    def transactional(self, callback):
        async def run(transaction):
            for _ in range(transaction.max_attempts):
                transaction.reads, transaction.writes = {}, {}
                self.attempts += 1
                self.in_transaction = True
                try:
                    result = await callback(transaction)
                    async with self.lock:
                        if self.fail_commit:
                            raise TimeoutError("Fallo sintético de commit")
                        if self.retry_once:
                            self.retry_once = False
                            raise Conflict()
                        if any(self.versions.get(k, 0) != v for k, v in transaction.reads.items()):
                            raise Conflict()
                        for path, data in transaction.writes.items():
                            self.docs[path] = deepcopy(data)
                            self.versions[path] = self.versions.get(path, 0) + 1
                    return result
                except Conflict:
                    continue
                finally:
                    self.in_transaction = False
            raise Conflict("Se agotaron los intentos transaccionales")

        return run


class Document:
    def __init__(self, client, path):
        self.client, self.path = client, path

    async def get(self, *, transaction=None, timeout=None, retry=None):
        assert timeout == 3 and retry is None
        if transaction is not None:
            assert not transaction.writes, "Firestore prohíbe lecturas después de escrituras"
            transaction.reads[self.path] = self.client.versions.get(self.path, 0)
        data = deepcopy(self.client.docs.get(self.path))
        await asyncio.sleep(0)
        return SimpleNamespace(exists=data is not None, to_dict=lambda: data)


class Collection:
    def __init__(self, client, path, filter_=None):
        self.client, self.path, self.filter = client, path, filter_

    def document(self, identifier):
        assert "/" not in identifier and len(identifier) == 64
        return Document(self.client, self.path + "/" + identifier)

    def where(self, *, filter):
        return Collection(self.client, self.path, filter)

    async def stream(self, *, transaction=None, timeout=None, retry=None):
        assert timeout == 3 and retry is None
        if transaction:
            assert not transaction.writes
        for path, data in list(self.client.docs.items()):
            if not path.startswith(self.path + "/") or path.count("/") != self.path.count("/") + 1:
                continue
            if self.filter and data.get(self.filter.field_path) != self.filter.value:
                continue
            snapshot = await Document(self.client, path).get(
                transaction=transaction, timeout=timeout, retry=retry
            )
            yield snapshot


class Transaction:
    def __init__(self, client, max_attempts):
        self.client, self.max_attempts = client, max_attempts
        self.reads, self.writes = {}, {}

    def set(self, document, data):
        self.writes[document.path] = deepcopy(data)


def store(client, owner="email"):
    from common.firestore_workflows import FirestoreWorkflowStore

    return FirestoreWorkflowStore(
        "example-project",
        "colombia-a2a-" + ("itinerary" if owner == "tourism" else owner),
        owner,
        client=client,
        transactional=client.transactional,
    )


async def test_firestore_restart_replay_history_and_immutable_continuation():
    client = FakeClient()
    first = store(client)
    claim = await first.begin("r/with/path", "s", {"message": "Borrador"}, now=100)
    response = {
        "status": "preview",
        "context": "signed",
        "preview": {"text": "A", "expires_at": 400},
    }
    await first.finish(claim, response, now=100, history=[{"role": "human", "content": "Hola"}])
    await first.finish(claim, response, now=100, history=[{"role": "human", "content": "Hola"}])
    reopened = store(client)
    assert (await reopened.snapshot("r/with/path", "s"))["approval"]["expires_at"] == 400
    assert await reopened.history("r/with/path", "s") == [{"role": "human", "content": "Hola"}]
    assert (await reopened.resolve("signed"))["version"] == 1
    assert await reopened.replay("r/with/path", "s", {"message": "Borrador"}) == response
    revision = await reopened.begin("r/with/path", "s", {"message": "Otra redacción"}, now=101)
    await reopened.finish(revision, response, now=101)
    assert (await reopened.resolve("signed"))["version"] == 1
    assert (await reopened.snapshot("r/with/path", "s"))["version"] == 2
    assert await reopened.snapshot("r/with/path", "other") is None
    assert not await reopened.session_matches("r/with/path", "other")
    assert await reopened.history("r/with/path", "other") == []


async def test_firestore_concurrent_reservation_and_retry_never_repeat_effect():
    client = FakeClient()
    a, b = store(client), store(client)
    client.retry_once = True
    claims = await asyncio.gather(
        a.begin("r", "s", {"message": "Envía"}, now=100),
        b.begin("r", "s", {"message": "Envía"}, now=100),
    )
    assert sorted(c["action"] for c in claims) == ["execute", "unknown"]
    assert client.attempts >= 3
    assert (await store(client).begin("r", "s", {"message": "Envía"}, now=999999))[
        "action"
    ] == "unknown"


async def test_firestore_finish_failure_preserves_reservation_and_session_cas():
    client = FakeClient()
    db = store(client)
    claim = await db.begin("r", "s", {"message": "Envía"}, now=100)
    client.fail_commit = True
    with pytest.raises(TimeoutError):
        await db.finish(claim, {"status": "sent", "delivery": "provider_accepted"}, now=101)
    client.fail_commit = False
    assert (await store(client).begin("r", "s", {"message": "Envía"}, now=9999))[
        "action"
    ] == "unknown"
    with pytest.raises(ValueError):
        await db.finish({**claim, "session_id": "other"}, {"status": "sent"})
    assert (await db.snapshot("r", "s"))["state"] == "unknown"


async def test_firestore_global_operation_ids_and_exact_approval_cannot_be_relabelled():
    client = FakeClient()
    db = store(client)
    original = await db.begin("r", "s", {"message": "A"}, operation_id="global/op", now=100)
    response = {
        "status": "preview",
        "preview": {"text": "A", "expires_at": 200},
        "approval_token": "T",
    }
    await db.finish(original, response, now=100)
    assert (await db.begin("other", "s", {"message": "A"}, operation_id="global/op"))[
        "action"
    ] == "stale"
    assert (await db.begin("r", "bad", {"message": "B"}))["action"] == "session_mismatch"
    assert (await db.begin("r", "s", {"message": "Sí"}, confirmation=True, now=201))[
        "action"
    ] == "expired"
    revision = await db.begin("r", "s", {"message": "B"}, expected_version=1, now=101)
    await db.finish(revision, {**response, "preview": {"text": "B", "expires_at": 400}})
    collision = await db.begin("r", "s", {"message": "C"}, operation_id="global/op", now=102)
    assert collision["action"] == "stale" and collision["version"] == 2
    old = {"action": "send", "preview": response["preview"], "approval_token": "T"}
    assert (await db.begin("r", "s", old, confirmation=True, expected_version=2, now=102))[
        "action"
    ] == "approval_mismatch"
    assert (await db.begin("r", "s", {"message": "Sí"}, context="old", now=102))[
        "action"
    ] == "stale"


async def test_firestore_audit_input_order_and_bounded_history_without_cross_session():
    client = FakeClient()
    db = store(client)
    from common.contracts import EmailRequest

    for index in range(12):
        claim = await db.begin("r", "s", {"message": str(index)}, now=100 + index)
        request = EmailRequest(
            request_id="r",
            session_id="s",
            operation_id=claim["operation_id"],
            plan_version=claim["version"],
        )
        for _ in range(8):
            await db.record_tool_failure(request, {"phase": "arguments"})
        await db.finish(
            claim,
            {"status": "completed"},
            now=100 + index,
            history=[{"role": "human", "content": str(index) + "x" * 1800}],
        )
    inputs = await db.recent_inputs("r", "s")
    assert inputs[0]["message"] == "11" and inputs[-1]["message"] == "0"
    assert len(inputs[0]["_tool_failures"]) == 6
    history = await db.history("r", "s")
    from common.workflows import encoded

    assert len(history) <= 16 and len(encoded(history)) <= 16000
    assert await db.recent_inputs("r", "bad") == []


async def test_firestore_runner_never_executes_providers_in_transaction_and_reopens(tmp_path):
    from common.contracts import EmailRequest, EmailResponse
    from common.workflows import WorkflowRunner

    client = FakeClient()
    runner = WorkflowRunner(None, "email", EmailResponse, store=store(client))
    effects = []

    async def execute(prepared):
        assert not client.in_transaction
        effects.append(prepared)
        return EmailResponse(
            status="sent", delivery="provider_accepted", message="Recibo sintético"
        )

    request = EmailRequest(message="Envía", request_id="r", session_id="s")
    sent = await runner.run(request, execute)
    reopened = WorkflowRunner(None, "email", EmailResponse, store=store(client))
    replay = await reopened.run(request, execute)
    assert sent == replay and len(effects) == 1
    assert not list(tmp_path.iterdir())


def test_firestore_settings_do_not_construct_sqlite_or_default_database(monkeypatch):
    from common.config import Settings
    from common.contracts import EmailResponse
    from common.workflows import WorkflowRunner

    monkeypatch.setenv("WORKFLOW_STORE", "firestore")
    monkeypatch.setenv("WORKFLOW_FIRESTORE_PROJECT", "example-project")
    monkeypatch.setenv("WORKFLOW_FIRESTORE_DATABASE", "colombia-a2a-email")
    monkeypatch.setenv("WORKFLOW_DB_PATH", "/work/forbidden.sqlite")
    settings = Settings.from_env("email")
    assert settings.workflow_store == "firestore" and settings.workflow_db_path == ""
    monkeypatch.setattr("common.workflows.WorkflowStore", lambda *a, **k: pytest.fail("SQLite"))
    client = FakeClient()
    import common.firestore_workflows as module

    monkeypatch.setattr(module, "FirestoreWorkflowStore", lambda *a, **k: client)
    assert WorkflowRunner.from_settings(settings, "email", EmailResponse).store is client
    monkeypatch.delenv("WORKFLOW_FIRESTORE_DATABASE")
    with pytest.raises(ValueError):
        Settings.from_env("email")


async def test_firestore_official_async_sdk_contract_without_adc_or_network():
    from unittest.mock import AsyncMock

    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import AsyncClient
    from google.cloud.firestore_v1.types import (
        BatchGetDocumentsResponse,
        BeginTransactionResponse,
        CommitResponse,
    )

    from common.firestore_workflows import FirestoreWorkflowStore

    client = AsyncClient(
        project="example-project", database="colombia-a2a-email", credentials=AnonymousCredentials()
    )
    calls = []

    async def batch_get_documents(*, request, metadata, retry, timeout):
        assert retry is None and timeout == 3
        calls.append(request)

        async def results():
            yield BatchGetDocumentsResponse(missing=request["documents"][0])

        return results()

    # Sólo el transporte RPC se sustituye: documentos, transacciones y decorador son oficiales.
    client._firestore_api_internal = SimpleNamespace(
        begin_transaction=AsyncMock(return_value=BeginTransactionResponse(transaction=b"test")),
        batch_get_documents=batch_get_documents,
        commit=AsyncMock(return_value=CommitResponse()),
        rollback=AsyncMock(),
    )
    db = FirestoreWorkflowStore("example-project", "colombia-a2a-email", "email", client=client)
    claim = await db.begin("r", "s", {"message": "Mensaje sintético"}, now=1)
    assert claim["action"] == "execute"
    rpc = client._firestore_api_internal
    assert rpc.begin_transaction.await_count == rpc.commit.await_count == 1
    commit = rpc.commit.call_args.kwargs["request"]
    assert commit["database"] == "projects/example-project/databases/colombia-a2a-email"
    assert len(commit["writes"]) == 3 and len(calls) == 3
    assert all("/databases/colombia-a2a-email/" in item["documents"][0] for item in calls)


async def test_firestore_finish_retry_and_ambiguous_commit_never_repeat_provider():
    from common.contracts import EmailRequest, EmailResponse
    from common.workflows import WorkflowRunner

    client = FakeClient()
    runner = WorkflowRunner(None, "email", EmailResponse, store=store(client))
    effects = []

    async def execute(request):
        effects.append(request.operation_id)
        client.retry_once = True
        return EmailResponse(
            status="sent", delivery="provider_accepted", message="Recibo sintético"
        )

    request = EmailRequest(message="Mensaje sintético", request_id="r", session_id="s")
    response = await runner.run(
        request, execute, history=lambda _: [{"role": "human", "content": "Una vez"}]
    )
    assert response.status == "sent" and len(effects) == 1
    assert len(await runner.call("history", "r", "s")) == 1
    assert (await runner.run(request, execute)).status == "sent" and len(effects) == 1

    second = request.model_copy(update={"message": "Otra orden", "plan_version": 1})

    async def lost_receipt(prepared):
        effects.append(prepared.operation_id)
        client.fail_commit = True
        return EmailResponse(
            status="sent", delivery="provider_accepted", message="Recibo sintético"
        )

    with pytest.raises(TimeoutError):
        await runner.run(second, lost_receipt)
    client.fail_commit = False
    assert (await runner.run(second, execute)).error_code == "workflow_stale"
    second = second.model_copy(update={"plan_version": 2})
    assert (await runner.run(second, execute)).status == "delivery_unknown" and len(effects) == 2


def test_sqlite_export_projection_preserves_literal_state_and_reserved_operations():
    from common.firestore_workflows import document_key, sqlite_documents
    from common.workflows import encoded

    response = encoded({"status": "sent", "context": "firma inalterada"})
    requests = [
        {
            "id": "r/1",
            "session": "s",
            "version": 2,
            "state": "running",
            "response": response,
            "approval": '{"token":"firma"}',
            "plan": '{"subject":"Texto"}',
            "updated": 10.0,
        }
    ]
    operations = [
        {
            "id": "op/1",
            "request_id": "r/1",
            "owner": "email",
            "version": 2,
            "digest": "original",
            "state": "reserved",
            "input": '{"message":"original"}',
            "receipt": None,
            "started": 9.0,
            "finished": None,
        }
    ]
    continuations = [{"digest": "a" * 64, "request_id": "r/1", "session": "s", "version": 1}]
    histories = [
        {"request_id": "r/1", "session": "s", "messages": '[{"role":"human","content":"Hola"}]'}
    ]
    docs = sqlite_documents(requests, operations, continuations, histories)
    row = docs["requests/" + document_key("r/1")]
    assert row["response"] == response and row["approval"] == requests[0]["approval"]
    assert row["pending"] == "op/1" and row["history"][0]["content"] == "Hola"
    assert docs["operations/" + document_key("op/1")] == operations[0]
    assert docs["continuations/" + "a" * 64] == continuations[0]
    older = {
        **operations[0],
        "id": "old",
        "version": 1,
        "state": "completed",
        "receipt": response,
        "started": 100.0,
    }
    # La versión manda aunque las filas y el reloj estén fuera de orden.
    docs = sqlite_documents(requests, [operations[0], older], continuations, histories)
    from common.firestore_workflows import digest_key

    assert docs["digests/" + digest_key("r/1", "original")] == {"operation_id": "op/1"}
    assert docs["requests/" + document_key("r/1")]["pending"] == "op/1"


async def test_firestore_native_a2a_and_protected_status_resume_without_client_context(monkeypatch):
    import httpx
    from langchain_core.messages import AIMessage
    from semantic_fixtures import conversation
    from test_email_chat_graph import SemanticModel
    from test_tourism_conversation import Conversation
    from test_workflow_integration import Transport

    import common.firestore_workflows as module
    from common.a2a_transport import send_typed
    from common.config import Settings
    from common.contracts import EmailResponse, TourismRequest
    from email_agent.app import create_app as email_app
    from tourism_agent.app import create_app as tourism_app
    from tourism_agent.service import TourismService

    implementation = module.FirestoreWorkflowStore
    clients = {"email": FakeClient(), "tourism": FakeClient()}

    def factory(project, database, owner):
        return implementation(
            project,
            database,
            owner,
            client=clients[owner],
            transactional=clients[owner].transactional,
        )

    monkeypatch.setattr(module, "FirestoreWorkflowStore", factory)

    def settings(role):
        return Settings.offline(
            workflow_store="firestore",
            workflow_firestore_project="example-project",
            workflow_firestore_database="colombia-a2a-" + role,
            public_url="http://127.0.0.1:" + ("8002" if role == "email" else "8001"),
            auth_required=True,
            service_api_key=role + "-key",
            approval_secret="s" * 40,
        )

    transport = Transport()
    app = email_app(settings("email"))
    app.state.service.transport, app.state.service.model = transport, SemanticModel("send")
    peers, errors = [], []

    async def peer(request):
        peers.append(request)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
            ) as client:
                result, _ = await send_typed(
                    "http://127.0.0.1:8002",
                    request.model_dump(mode="json"),
                    http_client=client,
                    api_key="email-key",
                )
        except Exception as error:
            errors.append((type(error).__name__, str(error)))
            raise
        return EmailResponse.model_validate(result)

    request = TourismRequest(
        message="Bogota 2 dias cultura; envia a student@example.com",
        request_id="joint",
        session_id="s",
    )
    result = await TourismService(
        settings("tourism"),
        peer=peer,
        conversation_model=conversation("generar_itinerario", "consultar_correo"),
    ).handle(request)
    assert result.email.status == "simulated" and len(transport.sent) == len(peers) == 1, errors
    native = Conversation(AIMessage(content="De nada."))
    reopened = TourismService(settings("tourism"), peer=peer, conversation_model=native)
    thanks = await reopened.handle(
        TourismRequest(
            message="Gracias", request_id="joint", session_id="s", plan_version=result.plan_version
        )
    )
    assert thanks.itinerary == result.itinerary and len(transport.sent) == 1
    assert len(await reopened.workflow.call("history", "joint", "s")) == 4
    server = tourism_app(settings("tourism"), service=reopened)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://local"
    ) as client:
        assert (await client.get("/api/requests/joint?session_id=s")).status_code == 401
        status = await client.get(
            "/api/requests/joint?session_id=s", headers={"X-API-Key": "tourism-key"}
        )
        assert (
            status.status_code == 200
            and status.json()["response"]["email"]["status"] == "simulated"
        )
        assert (
            await client.get(
                "/api/requests/joint?session_id=other", headers={"X-API-Key": "tourism-key"}
            )
        ).status_code == 404
    replay = await reopened.handle(request)
    assert replay.email.status == "simulated" and len(transport.sent) == len(peers) == 1
