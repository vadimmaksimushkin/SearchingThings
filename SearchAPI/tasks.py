import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)

SHUTDOWN_TASK_TIMEOUT = 30.0

# Detached background tasks (e.g. Google upserts) hold references here so they
# survive past the request that spawned them. The done-callback removes them on
# completion so the set doesn't grow forever
pending_upserts: set[asyncio.Task[None]] = set()


def detach(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(coro)
    pending_upserts.add(task)
    task.add_done_callback(pending_upserts.discard) # remove task from list
    return task


async def wait_for_pending(timeout_seconds: float = SHUTDOWN_TASK_TIMEOUT) -> None:
    """Await in-flight detached tasks on shutdown, bounded by timeout."""
    if not pending_upserts:
        return
    log.info(f"awaiting {len(pending_upserts)} in-flight detached task(s)")
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(*list(pending_upserts), return_exceptions=True)
    except TimeoutError:
        log.warning(f"timeout waiting for detached tasks after {timeout_seconds}s")
