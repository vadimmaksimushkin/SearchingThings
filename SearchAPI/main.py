import sys
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import orjson
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from SearchAPI.models import DoneEvent, ErrorEvent, Place, PlaceDetail, PlacePreviewEvent
from SearchAPI.search_local_db import (
    create_pool,
    fetch_place_detail,
    find_places_circle,
    find_places_rectangle,
)
from SearchAPI.search_by_location import Location


log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    log.info("asyncpg pool created")
    try:
        yield
    finally:
        await app.state.pool.close()
        log.info("asyncpg pool closed")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/searchByLocation")
async def search_by_location(
    request: Request,
    main_type: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(..., gt=0, le=50000),
    is_rectangle: bool = True,
    local_only: bool = True,
    max_results: int = Query(10, ge=1, le=2000),
) -> list[Place]:
    log.info(
        f"mainType={main_type}, lat={lat}, lon={lon}, radius={radius}, "
        f"localOnly={local_only}, maxResults={max_results}"
    )
    location = Location.from_center_point((lat, lon), radius, is_rectangle=is_rectangle)
    search = find_places_rectangle if is_rectangle else find_places_circle
    return await search(
        request.app.state.pool,
        location,
        main_type=main_type,
        max_results=max_results,
    )


@app.get("/place/{place_id}")
async def get_place(request: Request, place_id: str) -> PlaceDetail:
    place = await fetch_place_detail(request.app.state.pool, place_id)
    if place is None:
        raise HTTPException(status_code=404, detail=f"Place {place_id!r} not found")
    return place


def _ndjson(event: BaseModel) -> bytes:
    return orjson.dumps(event.model_dump()) + b"\n"


async def _stream_search(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int,
    is_rectangle: bool,
) -> AsyncIterator[bytes]:
    try:
        search = find_places_rectangle if is_rectangle else find_places_circle
        places = await search(pool, location, main_type=main_type, max_results=max_results)
        for place in places:
            yield _ndjson(PlacePreviewEvent(place=place))
        yield _ndjson(DoneEvent())
    except Exception as e:
        log.exception("search stream failed")
        yield _ndjson(ErrorEvent(message=str(e)))


@app.get("/searchStream")
async def search_stream(
    request: Request,
    main_type: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(..., gt=0, le=50000),
    is_rectangle: bool = True,
    max_results: int = Query(10, ge=1, le=2000),
) -> StreamingResponse:
    log.info(
        f"[stream] mainType={main_type}, lat={lat}, lon={lon}, radius={radius}, "
        f"isRectangle={is_rectangle}, maxResults={max_results}"
    )
    location = Location.from_center_point((lat, lon), radius, is_rectangle=is_rectangle)
    return StreamingResponse(
        _stream_search(request.app.state.pool, location, main_type, max_results, is_rectangle),
        media_type="application/x-ndjson",
    )
