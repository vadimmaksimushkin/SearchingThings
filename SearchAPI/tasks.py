import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)

SHUTDOWN_TASK_TIMEOUT = 30.0
SHUTDOWN_POPULATE_TIMEOUT = 10*60.0

# Detached background tasks (e.g. Google upserts) hold references here so they
# survive past the request that spawned them. The done-callback removes them on
# completion so the set doesn't grow forever
pending_upserts: set[asyncio.Task[None]] = set()
populating_tasks: set[asyncio.Task[None]] = set()


def detach(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(coro)
    pending_upserts.add(task)
    task.add_done_callback(pending_upserts.discard) # remove task from list
    return task


def detach_populate(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(coro)
    populating_tasks.add(task)
    task.add_done_callback(populating_tasks.discard)
    return task


async def await_set(
    tasks: set[asyncio.Task[None]], timeout_seconds: float, label: str,
) -> None:
    if not tasks:
        return
    log.info(f"awaiting {len(tasks)} in-flight {label} task(s)")
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(*list(tasks), return_exceptions=True)
    except TimeoutError:
        log.warning(f"timeout waiting for {label} tasks after {timeout_seconds}s")


async def wait_for_pending(timeout_seconds: float = SHUTDOWN_TASK_TIMEOUT) -> None:
    """Await in-flight detached upsert tasks on shutdown, bounded by timeout."""
    await await_set(pending_upserts, timeout_seconds, "upsert")


async def wait_for_populating(
    timeout_seconds: float = SHUTDOWN_POPULATE_TIMEOUT,
) -> None:
    """Await in-flight populate tasks on shutdown, bounded by timeout.
    Cancel-on-timeout leaves partial upserts in place (idempotent) and the
    advisory lock auto-releases when the DB connection drops."""
    await await_set(populating_tasks, timeout_seconds, "populate")
