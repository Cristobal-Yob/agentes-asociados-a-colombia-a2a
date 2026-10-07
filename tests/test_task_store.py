from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus

from common.a2a_transport import BoundedTaskStore


async def test_native_task_store_evicts_after_bound():
    store = BoundedTaskStore()
    context = ServerCallContext()
    for n in range(130):
        await store.save(
            Task(
                id=f"task-{n}",
                context_id="class",
                status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
            ),
            context,
        )
    assert await store.get("task-0", context) is None
    assert await store.get("task-129", context) is not None
