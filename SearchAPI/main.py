import asyncio
import asyncpg
import os
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
    FALLBACK_LANG,
    create_pool,
    fetch_place_detail,
    find_places_circle,
    find_places_rectangle,
    load_main_types,
)
from SearchAPI.search_stream import SEARCH_LANG_CODE, stream_search
from SearchAPI.tasks import wait_for_pending, wait_for_populating


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
    """Retry asyncpg.create_pool + load_main_types with exponential backoff
    until both succeed. Sets app.state.pool and app.state.main_types when
    ready. Cancelled on shutdown."""
    delay = 5.0
    while True:
        try:
            pool = await create_pool()
            main_types = await load_main_types(pool)
            app.state.pool = pool
            app.state.main_types = main_types
            return
        except Exception:
            log.exception(f"[ensure_pool] init failed; retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = None
    app.state.main_types = None
    pool_task = asyncio.create_task(ensure_pool(app))
    try:
        yield
    finally:
        pool_task.cancel()
        await wait_for_pending()
        await wait_for_populating()
        if app.state.pool is not None:
            await app.state.pool.close() # pyright: ignore[reportGeneralTypeIssues]
            log.info("asyncpg pool closed")


app = FastAPI(lifespan=lifespan, root_path=os.environ.get("ROOT_PATH", ""))
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


def get_main_types(request: Request) -> dict[str, dict[str, str]]:
    main_types = request.app.state.main_types
    if main_types is None:
        raise HTTPException(
            status_code=503,
            detail=DB_INIT_DETAIL,
            headers={"Retry-After": RETRY_AFTER},
        )
    return main_types


def validate_main_type(main_type: str, main_types: dict[str, dict[str, str]]) -> None:
    if main_type not in main_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown main_type {main_type!r}",
        )


def text_query_for(main_types: dict[str, dict[str, str]], main_type: str) -> str:
    """Pick the localized textQuery for Google's searchText, falling back to
    FALLBACK_LANG if SEARCH_LANG_CODE isn't present for this type."""
    labels = main_types[main_type]
    return labels.get(SEARCH_LANG_CODE, labels[FALLBACK_LANG])


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/main_types")
async def list_main_types(
    main_types: dict[str, dict[str, str]] = Depends(get_main_types),
) -> dict[str, dict[str, str]]:
    return main_types


@app.get("/searchByLocation")
async def search_by_location(
    main_type: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(..., gt=0, le=50000),
    is_rectangle: bool = True,
    local_only: bool = True,
    max_results: int = Query(10, ge=1, le=2000),
    pool: asyncpg.Pool = Depends(get_pool),
    main_types: dict[str, dict[str, str]] = Depends(get_main_types),
) -> list[Place]:
    validate_main_type(main_type, main_types)
    log.info(
        f"mainType={main_type}, lat={lat}, lon={lon}, radius={radius}, "
        f"localOnly={local_only}, maxResults={max_results}"
    )
    location = Location.from_center_point((lat, lon), radius, is_rectangle=is_rectangle)
    search = find_places_rectangle if is_rectangle else find_places_circle
    return [
        p async for p in search(
            pool, location, main_type=main_type, max_results=max_results,
        )
    ]


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
    main_types: dict[str, dict[str, str]] = Depends(get_main_types),
) -> StreamingResponse:
    validate_main_type(main_type, main_types)
    log.info(
        f"[stream] mainType={main_type}, lat={lat}, lon={lon}, radius={radius}, "
        f"isRectangle={is_rectangle}, localOnly={local_only}, "
        f"includeReviews={include_reviews}, includePhotos={include_photos}, "
        f"maxResults={max_results}"
    )
    location = Location.from_center_point((lat, lon), radius, is_rectangle=is_rectangle)
    return StreamingResponse(
        stream_search(
            pool, location, main_type, text_query_for(main_types, main_type),
            max_results, is_rectangle,
            local_only, include_reviews, include_photos,
        ),
        media_type="application/x-ndjson",
    )
