import asyncio
import time
from collections import OrderedDict
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.server.agent_execution import AgentExecutor
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    Artifact,
    Message,
    Part,
    Role,
    SecurityRequirement,
    SecurityScheme,
    SendMessageRequest,
    Task,
    TaskNotCancelableError,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from pydantic import ValidationError

from common.config import checked_base
from common.contracts import EmailResponse


def data_part(payload):
    return Part(data=ParseDict(payload, Value()), media_type="application/json")


class BoundedTaskStore(InMemoryTaskStore):
    """Ephemeral native records only: 128 entries, ten-minute inactivity TTL."""

    def __init__(self):
        super().__init__()
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()

    async def prune(self):
        now = time.monotonic()
        for key, (deadline, context) in list(self.entries.items()):
            if deadline <= now or len(self.entries) > 128:
                await super().delete(key[1], context)
                self.entries.pop(key, None)

    async def save(self, task, context):
        async with self.lock:
            await super().save(task, context)
            key = (resolve_user_scope(context), task.id)
            self.entries[key] = (time.monotonic() + 600, context)
            self.entries.move_to_end(key)
            await self.prune()

    async def get(self, task_id, context):
        async with self.lock:
            await self.prune()
            return await super().get(task_id, context)

    async def list(self, params, context):
        async with self.lock:
            await self.prune()
            return await super().list(params, context)

    async def delete(self, task_id, context):
        async with self.lock:
            await super().delete(task_id, context)
            self.entries.pop((resolve_user_scope(context), task_id), None)


class Executor(AgentExecutor):
    def __init__(self, service, request_type):
        self.service = service
        self.request_type = request_type

    async def execute(self, context, event_queue):
        try:
            data = [
                MessageToDict(part.data) for part in context.message.parts if part.HasField("data")
            ]
            if len(data) != 1:
                raise ValueError("Se exige exactamente una parte de datos JSON")
            request = self.request_type.model_validate(data[0])
            result = (await self.service.handle(request)).model_dump(mode="json")
            state = TaskState.TASK_STATE_COMPLETED
            if result["status"] in {"preview", "clarification", "conflict"}:
                state = TaskState.TASK_STATE_INPUT_REQUIRED
            elif result["status"] in {"error", "delivery_unknown", "creation_unknown"}:
                state = TaskState.TASK_STATE_FAILED
        except (ValueError, ValidationError, TypeError):
            result = {
                "status": "error",
                "message": (
                    "Los datos del agente no cumplen el contrato; consulte el esquema de entrada."
                ),
                "error_code": "invalid_payload",
            }
            state = TaskState.TASK_STATE_FAILED
        task = Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=state),
            artifacts=[
                Artifact(artifact_id=str(uuid4()), name="result", parts=[data_part(result)])
            ],
        )
        await event_queue.enqueue_event(task)

    async def cancel(self, context, event_queue):
        # Short synchronous tasks have no safe cancellation after a send may start.
        raise TaskNotCancelableError(
            "Estas tareas síncronas de duración limitada no admiten cancelación."
        )


def mount_a2a(app, service, request_type, settings, name, description, skills):
    card = AgentCard(
        security_schemes={
            "service_key": SecurityScheme(
                api_key_security_scheme=APIKeySecurityScheme(
                    location="header",
                    name="X-API-Key",
                    description="Clave privada de este servicio",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"service_key": {"list": []}})],
        name=name,
        description=description + " No admite cancelación. Las tareas se almacenan temporalmente; "
        "el cliente debe conservar el contexto tipado.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=settings.public_url + "/a2a", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(id=id_, name=label, description=desc, tags=[id_])
            for id_, label, desc in skills
        ],
    )
    handler = DefaultRequestHandler(Executor(service, request_type), BoundedTaskStore(), card)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, "/a2a"),
    )
    return card


async def send_typed(
    base_url, payload, *, iam=False, task_id="", context_id="", http_client=None, api_key=""
):
    """Official SDK discovery + SendMessage; card URLs must exactly match configured peer."""
    base = checked_base(base_url)
    headers = {"X-API-Key": api_key} if api_key else {}
    if iam:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        token = await asyncio.to_thread(id_token.fetch_id_token, Request(), base)
        headers["Authorization"] = f"Bearer {token}"
    owned = http_client is None
    client = http_client or httpx.AsyncClient(timeout=12, follow_redirects=False, headers=headers)
    previous_headers = dict(client.headers)
    if not owned:
        client.headers.update(headers)
    try:
        async with asyncio.timeout(15):
            card = await A2ACardResolver(client, base).get_agent_card()
            interfaces = list(card.supported_interfaces)
            if not interfaces or any(
                interface.url != base + "/a2a"
                or interface.protocol_binding != "JSONRPC"
                or interface.protocol_version != "1.0"
                for interface in interfaces
            ):
                raise ValueError(
                    "La interfaz de la tarjeta del agente está fuera de la lista permitida"
                )
            sdk = ClientFactory(
                ClientConfig(
                    streaming=False, httpx_client=client, supported_protocol_bindings=["JSONRPC"]
                )
            ).create(card)
            req = SendMessageRequest(
                message=Message(
                    message_id=str(uuid4()),
                    role=Role.ROLE_USER,
                    context_id=context_id,
                    task_id=task_id,
                    parts=[data_part(payload)],
                )
            )
            async for event in sdk.send_message(req):
                if not event.HasField("task") or not event.task.artifacts:
                    raise ValueError("El agente remoto no devolvió un artefacto de tarea")
                data = MessageToDict(event.task.artifacts[-1].parts[0].data)
                return data, event.task
            raise ValueError("El agente remoto no devolvió una respuesta")
    finally:
        if owned:
            await client.aclose()
        else:
            client.headers.clear()
            client.headers.update(previous_headers)


def email_peer(settings):
    async def delegate(request):
        data, task = await send_typed(
            settings.peer_url,
            request.model_dump(mode="json"),
            iam=settings.peer_iam,
            api_key=settings.email_peer_api_key,
        )
        data.update(a2a_task_id=task.id, a2a_context_id=task.context_id)
        return EmailResponse.model_validate(data)

    return delegate


def agenda_peer(settings):
    from agenda_agent.contracts import AgendaResponse

    async def delegate(request):
        data, task = await send_typed(
            settings.agenda_peer_url,
            request.model_dump(mode="json"),
            iam=settings.peer_iam,
            api_key=settings.agenda_peer_api_key,
        )
        data.update(a2a_task_id=task.id, a2a_context_id=task.context_id)
        return AgendaResponse.model_validate(data)

    return delegate
