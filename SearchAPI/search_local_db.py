import asyncio
import sys
from pathlib import Path
from typing import Any, Literal

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import PLACES_DB_URL
from SearchAPI.search_by_location import Location


PLACE_COLUMNS = (
    "place_id, main_type, name, address, phone, website, "
    "rating, rating_count, "
    "ST_Y(geog::geometry) AS latitude, ST_X(geog::geometry) AS longitude, "
    "plus_code, category, emails"
)

ORDER_BY_RATING = """
    COALESCE(rating >= 4, FALSE) DESC,
    CASE WHEN rating >= 4 THEN rating_count END DESC NULLS LAST,
    rating DESC NULLS LAST,
    rating_count DESC NULLS LAST
"""


async def create_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    return await asyncpg.create_pool(PLACES_DB_URL, min_size=min_size, max_size=max_size)


OrderBy = Literal["rating", "location"]


async def find_places_rectangle(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int = 10,
    order_by: OrderBy = "rating") -> list[dict[str, Any]]:
    if location.south_west is None or location.north_east is None:
        raise ValueError("Location has no bounding box (south_west / north_east)")
    if location.center_point is None:
        raise ValueError("Location has no center point")

    sw_lat, sw_lon = location.south_west
    ne_lat, ne_lon = location.north_east
    center_lat, center_lon = location.center_point

    query_order_by_rating = f"""
        SELECT {PLACE_COLUMNS}
        FROM places
        WHERE main_type = $1
          AND geog && ST_MakeEnvelope($2, $3, $4, $5, 4326)::geography
        ORDER BY {ORDER_BY_RATING}
        LIMIT $6
    """
    query_order_by_location = f"""
        SELECT {PLACE_COLUMNS}
        FROM places
        WHERE main_type = $1
          AND geog && ST_MakeEnvelope($2, $3, $4, $5, 4326)::geography
        ORDER BY geog <-> ST_MakePoint($6, $7)::geography ASC
        LIMIT $8
    """
    async with pool.acquire() as conn:
        if order_by == "rating":
            rows = await conn.fetch(
                query_order_by_rating,
                main_type, sw_lon, sw_lat, ne_lon, ne_lat, max_results,
            )
        else:
            rows = await conn.fetch(
                query_order_by_location,
                main_type, sw_lon, sw_lat, ne_lon, ne_lat,
                center_lon, center_lat, max_results,
            )
    return [dict(row) for row in rows]


async def find_places_circle(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int = 10,
    order_by: OrderBy = "location") -> list[dict[str, Any]]:
    if location.center_point is None or location.radius is None:
        raise ValueError("Location has no center point / radius for circle search")

    center_lat, center_lon = location.center_point
    radius = location.radius

    query_order_by_rating = f"""
        SELECT {PLACE_COLUMNS}
        FROM places
        WHERE main_type = $1
          AND ST_DWithin(geog, ST_MakePoint($2, $3)::geography, $4)
        ORDER BY {ORDER_BY_RATING}
        LIMIT $5
    """
    query_order_by_location = f"""
        SELECT {PLACE_COLUMNS}
        FROM places
        WHERE main_type = $1
          AND ST_DWithin(geog, ST_MakePoint($2, $3)::geography, $4)
        ORDER BY geog <-> ST_MakePoint($2, $3)::geography ASC
        LIMIT $5
    """
    async with pool.acquire() as conn:
        query = query_order_by_rating if order_by == "rating" else query_order_by_location
        rows = await conn.fetch(
            query,
            main_type, center_lon, center_lat, radius, max_results,
        )
    return [dict(row) for row in rows]


if __name__ == "__main__":
    async def _main() -> None:
        pool = await create_pool()
        try:
            CDMX_TEST = (19.412429, -99.1664120)

            rect_loc = Location.from_center_point(CDMX_TEST, 50_000, is_rectangle=True)
            circle_loc = Location.from_center_point(CDMX_TEST, 50_000, is_rectangle=False)
            rect_results = await find_places_rectangle(pool, rect_loc, main_type="gym", max_results=2_000)
            circle_results = await find_places_circle(pool, circle_loc, main_type="gym", max_results=2_000)
            print(len(rect_results))
            print(len(circle_results))
            print("done")
        finally:
            await pool.close()

    asyncio.run(_main())
