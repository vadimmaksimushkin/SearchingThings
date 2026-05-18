import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from SearchAPI.search_local_db import create_pool, find_places_circle, find_places_rectangle
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
) -> list[dict[str, Any]]:
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
