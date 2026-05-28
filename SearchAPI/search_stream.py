import asyncio
import logging
import orjson
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
import asyncpg
from pydantic import BaseModel
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from SearchAPI.google_fetch import (
    Location,
    google_text_search,
    photo_from_google,
    place_from_google,
    review_from_google,
)
from SearchAPI.models import (
    DoneEvent,
    ErrorEvent,
    PhotosEvent,
    PlacePreviewEvent,
    PlaceUpdateEvent,
    ReviewsEvent,
    StreamEvent,
)
from SearchAPI.local_db_query import (
    fetch_photos_for_ids,
    fetch_reviews_for_ids,
    find_places_circle,
    find_places_rectangle,
    upsert_photos,
    upsert_place,
    upsert_reviews,
)
from SearchAPI.tasks import detach
from constants import MAIN_TYPES
log = logging.getLogger(__name__)
SEARCH_LANG_CODE: str = "es"

def ndjson(event: BaseModel) -> bytes:
    return orjson.dumps(event.model_dump()) + b"\n"


async def upsert_google_place(
    pool: asyncpg.Pool, main_type: str, raw: dict[str, Any],
) -> bool:
    """Upsert place + reviews + photos in one transaction. True on success."""
    place_id = raw.get("id", "")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await upsert_place(conn, main_type, raw)
                await upsert_reviews(conn, place_id, raw.get("reviews"))
                await upsert_photos(conn, place_id, raw.get("photos"))
    except Exception:
        log.exception(f"[stage3] upsert failed for {place_id}")
        return False
    return True


async def emit_google_events(
    queue: asyncio.Queue[StreamEvent],
    raw: dict[str, Any],
    main_type: str,
    streamed_ids: set[str],
    include_reviews: bool,
    include_photos: bool) -> None:
    """Update the streamed Place info in case of fresh updated info from google.
    Event gets pushed to queue and streamed lately if success"""
    place_id = raw["id"]
    try:
        place = place_from_google(raw, main_type)
    except Exception:
        log.exception(f"[stage3] could not build Place from {place_id}")
        return
    is_update = place_id in streamed_ids
    await queue.put(PlaceUpdateEvent(place=place) if is_update else PlacePreviewEvent(place=place))
    if include_reviews and raw.get("reviews"):
        await queue.put(ReviewsEvent(
            place_id=place_id,
            items=[review_from_google(r) for r in raw["reviews"]],
        ))
    if include_photos and raw.get("photos"):
        await queue.put(PhotosEvent(
            place_id=place_id,
            items=[photo_from_google(p) for p in raw["photos"]],
        ))


async def google_producer(
    queue: asyncio.Queue[StreamEvent],
    pool: asyncpg.Pool,
    main_type: str,
    location: Location,
    is_rectangle: bool,
    streamed_ids: set[str],
    include_reviews: bool,
    include_photos: bool) -> None:
    """Stage 3. Run textSearch with live fields, upsert to DB, emit events"""
    async def handle_one(raw: dict[str, Any]) -> None:
        if not await upsert_google_place(pool, main_type, raw):
            return
        await emit_google_events(
            queue, raw, main_type, streamed_ids, include_reviews, include_photos,
        )
    try:
        places = await google_text_search(
            location,
            is_rectangle,
            text_query=MAIN_TYPES[main_type][SEARCH_LANG_CODE],
            live=True,
        )
        log.info(f"[stage3] textSearch live: {len(places)} places returned")
        await asyncio.gather(
            *(handle_one(raw) for raw in places if raw.get("id")),
            return_exceptions=True,
        )
    except Exception as e:
        log.exception("[stage3] producer failed")
        await queue.put(ErrorEvent(message=f"google textSearch live: {e}"))
    finally:
        await queue.put(None)


async def produce_reviews(
    pool: asyncpg.Pool,
    place_ids: list[str],
    queue: asyncio.Queue[ReviewsEvent | PhotosEvent | ErrorEvent | None],
) -> None:
    try:
        async for pid, items in fetch_reviews_for_ids(pool, place_ids):
            await queue.put(ReviewsEvent(place_id=pid, items=items))
    except Exception as e:
        log.exception("[stage1] reviews stream failed")
        await queue.put(ErrorEvent(message=f"reviews: {e}"))
    finally:
        await queue.put(None)


async def produce_photos(
    pool: asyncpg.Pool,
    place_ids: list[str],
    queue: asyncio.Queue[ReviewsEvent | PhotosEvent | ErrorEvent | None],
) -> None:
    try:
        async for pid, items in fetch_photos_for_ids(pool, place_ids):
            await queue.put(PhotosEvent(place_id=pid, items=items))
    except Exception as e:
        log.exception("[stage1] photos stream failed")
        await queue.put(ErrorEvent(message=f"photos: {e}"))
    finally:
        await queue.put(None)


async def stream_reviews_and_photos(
    pool: asyncpg.Pool, place_ids: list[str],
    include_reviews: bool, include_photos: bool,
) -> AsyncIterator[ReviewsEvent | PhotosEvent | ErrorEvent]:
    """Run reviews and photos cursors concurrently; merge events onto one stream."""
    queue: asyncio.Queue[ReviewsEvent | PhotosEvent | ErrorEvent | None] = asyncio.Queue()

    tasks: list[asyncio.Task[None]] = []
    if include_reviews:
        tasks.append(asyncio.create_task(produce_reviews(pool, place_ids, queue)))
    if include_photos:
        tasks.append(asyncio.create_task(produce_photos(pool, place_ids, queue)))
    if not tasks:
        return
    try:
        remaining = len(tasks)
        while remaining > 0:
            event = await queue.get()
            if event is None:
                remaining -= 1
            else:
                yield event
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def stage1_local(
    pool: asyncpg.Pool, location: Location, main_type: str,
    max_results: int, is_rectangle: bool,
    include_reviews: bool, include_photos: bool,
    streamed_ids: set[str],
) -> AsyncIterator[bytes]:
    """Stage 1. stream local-DB matches + optional batched reviews/photos."""
    search = find_places_rectangle if is_rectangle else find_places_circle
    local_ids: list[str] = []
    async for place in search(
        pool, location, main_type=main_type, max_results=max_results,
    ):
        streamed_ids.add(place.place_id)
        local_ids.append(place.place_id)
        yield ndjson(PlacePreviewEvent(place=place))

    if not local_ids or not (include_reviews or include_photos):
        return
    async for event in stream_reviews_and_photos(
        pool, local_ids, include_reviews, include_photos,
    ):
        yield ndjson(event)


async def stage2_3_google(
    pool: asyncpg.Pool, location: Location, main_type: str, is_rectangle: bool,
    streamed_ids: set[str], include_reviews: bool, include_photos: bool,
) -> AsyncIterator[bytes]:
    """Stage 2. Run textSearch IDs only query for new IDs
    Stage 3. If there more than N new IDs - runs the same 'live' query,
    Local DB gets updated and client receives updated data with new and updated
    place"""
    # stage 2
    try:
        id_results = await google_text_search(
            location,
            is_rectangle,
            text_query=MAIN_TYPES[main_type][SEARCH_LANG_CODE],
            live=False
            )
    except Exception as e:
        log.exception("textSearch stage failed")
        yield ndjson(ErrorEvent(message=f"google textSearch: {e}"))
        return
    new_count = sum(
        1 for p in id_results
        if p.get("id") and p["id"] not in streamed_ids
    )
    log.info(
        f"[stage2] textSearch: {len(id_results)} IDs returned, "
        f"{new_count} new (not in local results)"
    )
    if new_count <= 0:
        return
    # stage 3
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
    detach(google_producer(
        queue, pool, main_type, location, is_rectangle,
        streamed_ids, include_reviews, include_photos,
    ))
    # If client disconnects this generator dies but the detached task
    # keeps going and writes data to DB
    while True:
        event = await queue.get()
        if event is None:
            break
        yield ndjson(event)


async def stream_search(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int,
    is_rectangle: bool,
    local_only: bool,
    include_reviews: bool,
    include_photos: bool,
) -> AsyncIterator[bytes]:
    streamed_ids: set[str] = set()
    # FIXME: remove live block when ready
    if not local_only:
        local_only = True
    try:
        async for chunk in stage1_local(
            pool, location, main_type, max_results, is_rectangle,
            include_reviews, include_photos, streamed_ids,
        ):
            yield chunk
    except Exception as e:
        log.exception("local search stage failed")
        yield ndjson(ErrorEvent(message=f"local: {e}"))
        yield ndjson(DoneEvent())
        return

    if not local_only:
        async for chunk in stage2_3_google(
            pool, location, main_type, is_rectangle, streamed_ids,
            include_reviews, include_photos,
        ):
            yield chunk

    yield ndjson(DoneEvent())
