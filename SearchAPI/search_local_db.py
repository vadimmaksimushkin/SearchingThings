import asyncio
import sys
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import PLACES_DB_URL
from SearchAPI.search_by_location import Location


PLACE_COLUMNS = (
    "place_id, main_type, name, address, phone, website, "
    "rating, rating_count, latitude, longitude, plus_code, category, emails"
)


async def create_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    return await asyncpg.create_pool(PLACES_DB_URL, min_size=min_size, max_size=max_size)

# FIXME: proper rectangle/circle location handling
# FIXME: ORDER BY clarification
# FIXME: Optional pagination
async def find_places_in_square(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int = 10) -> list[dict[str, Any]]:
    if location.south_west is None or location.north_east is None:
        raise ValueError("Location has no bounding box (south_west / north_east)")
    if location.center_point is None:
        raise ValueError("Location has no center point for distance ordering")

    sw_lat, sw_lon = location.south_west
    ne_lat, ne_lon = location.north_east
    center_lat, center_lon = location.center_point

    query = f"""
        SELECT {PLACE_COLUMNS}
        FROM places
        WHERE main_type = $1
          AND latitude  BETWEEN $2 AND $3
          AND longitude BETWEEN $4 AND $5
        ORDER BY 6371000 * acos(least(1.0,
            cos(radians($6)) * cos(radians(latitude)) *
            cos(radians(longitude) - radians($7)) +
            sin(radians($6)) * sin(radians(latitude))
        )) ASC
        LIMIT $8
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            query, main_type, sw_lat, ne_lat, sw_lon, ne_lon,
            center_lat, center_lon, max_results,
        )
    return [dict(row) for row in rows]


if __name__ == "__main__":
    async def _main() -> None:
        pool = await create_pool()
        try:
            CDMX_TEST = (19.412429, -99.1664120)
            loc = Location.from_center_point(CDMX_TEST, 15_500, is_rectangle=True)
            results = await find_places_in_square(pool, loc, main_type="gym", max_results=20)
            print(f"Found {len(results)} places")
            for r in results:
                print(f"  - {r['name']!r} rating={r['rating']} count={r['rating_count']}")
        finally:
            await pool.close()

    asyncio.run(_main())
