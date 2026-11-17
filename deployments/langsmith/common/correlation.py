from pydantic import BaseModel, Field


class Correlation(BaseModel):
    request_id: str | None = Field(None, min_length=1, max_length=100, title="Solicitud durable")
    plan_version: int | None = Field(None, ge=1, title="Versión del plan")
    operation_id: str | None = Field(
        None, min_length=1, max_length=100, title="Operación idempotente"
    )


class ResultCorrelation(Correlation):
    session_id: str | None = Field(None, title="Sesión de la solicitud")
    workflow_state: str | None = Field(None, title="Estado durable")

    a2a_task_id: str | None = Field(None, title="Tarea A2A remota")
    a2a_context_id: str | None = Field(None, title="Contexto A2A remoto")
