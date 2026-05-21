import asyncio
import asyncpg
import sys
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from SearchAPI.models import Place, PlaceDetail
from SearchAPI.google_fetch import Location
from SearchAPI.local_db_query import (
    create_pool,
    fetch_place_detail,
    find_places_circle,
    find_places_rectangle,
)
from SearchAPI.search_stream import stream_search
from SearchAPI.tasks import wait_for_pending


log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


RETRY_AFTER = "10"
DB_DOWN_DETAIL = "Database unavailable"
DB_INIT_DETAIL = "Database initializing"


async def ensure_pool(app: FastAPI) -> None:
    """Retry asyncpg.create_pool with exponential backoff until it succeeds.
    Sets app.state.pool when ready. Cancelled on shutdown."""
    delay = 5.0
    while True:
        try:
            app.state.pool = await create_pool()
            log.info("asyncpg pool created")
            return
        except Exception as e:
            log.warning(f"pool creation failed: {e}; retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = None
    pool_task = asyncio.create_task(ensure_pool(app))
    try:
        yield
    finally:
        pool_task.cancel()
        await wait_for_pending()
        if app.state.pool is not None:
            await app.state.pool.close() # pyright: ignore[reportGeneralTypeIssues]
            log.info("asyncpg pool closed")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def db_error_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    try:
        return await call_next(request)
    except (asyncpg.PostgresError, OSError) as e:
        log.exception("DB error during request")
        return JSONResponse(
            status_code=503,
            content={"detail": f"{DB_DOWN_DETAIL}: {e}"},
            headers={"Retry-After": RETRY_AFTER},
        )


def get_pool(request: Request) -> asyncpg.Pool:
    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail=DB_INIT_DETAIL,
            headers={"Retry-After": RETRY_AFTER},
        )
    return pool


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/searchByLocation")
async def search_by_location(
    main_type: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(..., gt=0, le=50000),
    is_rectangle: bool = True,
    local_only: bool = True,
    max_results: int = Query(10, ge=1, le=2000),
    pool: asyncpg.Pool = Depends(get_pool)
) -> list[Place]:
    log.info(
        f"mainType={main_type}, lat={lat}, lon={lon}, radius={radius}, "
        f"localOnly={local_only}, maxResults={max_results}"
    )
    location = Location.from_center_point((lat, lon), radius, is_rectangle=is_rectangle)
    search = find_places_rectangle if is_rectangle else find_places_circle
    return await search(
        pool,
        location,
        main_type=main_type,
        max_results=max_results,
    )


@app.get("/place/{place_id}")
async def get_place(
    place_id: str,
    pool: asyncpg.Pool = Depends(get_pool)
) -> PlaceDetail:
    place = await fetch_place_detail(pool, place_id)
    if place is None:
        raise HTTPException(status_code=404, detail=f"Place {place_id!r} not found")
    return place


@app.get("/searchStream")
async def search_stream(
    main_type: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(..., gt=0, le=50000),
    is_rectangle: bool = True,
    local_only: bool = True,
    include_reviews: bool = False,
    include_photos: bool = False,
    max_results: int = Query(10, ge=1, le=2000),
    pool: asyncpg.Pool = Depends(get_pool),
) -> StreamingResponse:
    log.info(
        f"[stream] mainType={main_type}, lat={lat}, lon={lon}, radius={radius}, "
        f"isRectangle={is_rectangle}, localOnly={local_only}, "
        f"includeReviews={include_reviews}, includePhotos={include_photos}, "
        f"maxResults={max_results}"
    )
    location = Location.from_center_point((lat, lon), radius, is_rectangle=is_rectangle)
    return StreamingResponse(
        stream_search(
            pool, location, main_type, max_results, is_rectangle,
            local_only, include_reviews, include_photos,
        ),
        media_type="application/x-ndjson",
    )
