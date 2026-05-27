import asyncio
import asyncpg
import orjson
import sys
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any, Literal
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import PLACES_DB_URL, R2_PUBLIC_URL
from SearchAPI.models import Photo, Place, PlaceDetail, Review
from SearchAPI.google_fetch import Location, parse_published_at


PLACE_COLUMNS = (
    "place_id, main_type, name, address, phone, website, "
    "rating, rating_count, "
    "ST_Y(geog::geometry) AS latitude, ST_X(geog::geometry) AS longitude, "
    "plus_code, category, emails, preview_photo"
)

ORDER_BY_RATING = """
    COALESCE(rating >= 4, FALSE) DESC,
    CASE WHEN rating >= 4 THEN rating_count END DESC NULLS LAST,
    rating DESC NULLS LAST,
    rating_count DESC NULLS LAST
"""

QUERY_RECTANGLE_ORDER_BY_RATING = f"""
    SELECT {PLACE_COLUMNS}
    FROM places_with_preview
    WHERE main_type = $1
        AND geog && ST_MakeEnvelope($2, $3, $4, $5, 4326)::geography
    ORDER BY {ORDER_BY_RATING}
    LIMIT $6
"""
QUERY_RECTANGLE_ORDER_BY_LOCATION = f"""
    SELECT {PLACE_COLUMNS}
    FROM places_with_preview
    WHERE main_type = $1
        AND geog && ST_MakeEnvelope($2, $3, $4, $5, 4326)::geography
    ORDER BY geog <-> ST_MakePoint($6, $7)::geography ASC
    LIMIT $8
"""

QUERY_CIRCLE_ORDER_BY_RATING = f"""
    SELECT {PLACE_COLUMNS}
    FROM places_with_preview
    WHERE main_type = $1
        AND ST_DWithin(geog, ST_MakePoint($2, $3)::geography, $4)
    ORDER BY {ORDER_BY_RATING}
    LIMIT $5
"""
QUERY_CIRCLE_ORDER_BY_LOCATION = f"""
    SELECT {PLACE_COLUMNS}
    FROM places_with_preview
    WHERE main_type = $1
        AND ST_DWithin(geog, ST_MakePoint($2, $3)::geography, $4)
    ORDER BY geog <-> ST_MakePoint($2, $3)::geography ASC
    LIMIT $5
"""

REVIEW_COLUMNS = (
    "name, rating, text, language_code, "
    "author_name, author_uri, author_photo, "
    "published_at, flag_content_uri, google_maps_uri"
)

PHOTO_COLUMNS = "name, width_px, height_px, google_maps_uri, flag_content_uri, bucket_key, is_preview"

PLACE_QUERY = f"SELECT {PLACE_COLUMNS} FROM places_with_preview WHERE place_id = $1"


def add_preview_link_to_place(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Convert preview_photo from a bucket_key into a full R2 HTTPS URL"""
    bucket_key = row_dict.get("preview_photo")
    if bucket_key:
        row_dict["preview_photo"] = f"{R2_PUBLIC_URL}/{bucket_key}"
    return row_dict


def add_url_to_photo(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Convert photo bucket_key into a full R2 HTTPS URL."""
    bucket_key = row_dict.get("bucket_key")
    if bucket_key:
        row_dict["bucket_key"] = f"{R2_PUBLIC_URL}/{bucket_key}"
    return row_dict

REVIEWS_QUERY = f"""
    SELECT {REVIEW_COLUMNS}
    FROM reviews
    WHERE place_id = $1
    ORDER BY published_at DESC NULLS LAST
"""
PHOTOS_QUERY = f"SELECT {PHOTO_COLUMNS} FROM photos WHERE place_id = $1"

QUERY_FETCH_REVIEWS = f"""
    SELECT place_id, {REVIEW_COLUMNS}
    FROM reviews
    WHERE place_id = ANY($1::text[])
    ORDER BY place_id, published_at DESC NULLS LAST
"""

QUERY_FETCH_PHOTOS = f"""
    SELECT place_id, {PHOTO_COLUMNS}
    FROM photos
    WHERE place_id = ANY($1::text[])
    ORDER BY place_id
"""

UPSERT_PLACE_SQL = """
INSERT INTO places (
    place_id, main_type, name, address, phone, website,
    rating, rating_count, geog, plus_code, category,
    opening_hours, secondary_opening_hours
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, ST_SetSRID(ST_MakePoint($9, $10), 4326)::geography,
    $11, $12, $13, $14
)
ON CONFLICT (place_id) DO UPDATE SET
    main_type               = EXCLUDED.main_type,
    name                    = EXCLUDED.name,
    address                 = EXCLUDED.address,
    phone                   = EXCLUDED.phone,
    website                 = EXCLUDED.website,
    rating                  = EXCLUDED.rating,
    rating_count            = EXCLUDED.rating_count,
    geog                    = EXCLUDED.geog,
    plus_code               = EXCLUDED.plus_code,
    category                = EXCLUDED.category,
    opening_hours           = EXCLUDED.opening_hours,
    secondary_opening_hours = EXCLUDED.secondary_opening_hours,
    fetched_at              = now()
"""

UPSERT_REVIEW_SQL = """
INSERT INTO reviews (
    place_id, name, rating, text, language_code,
    author_name, author_uri, author_photo,
    published_at, flag_content_uri, google_maps_uri, raw
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (place_id, name) DO UPDATE SET
    rating           = EXCLUDED.rating,
    text             = EXCLUDED.text,
    language_code    = EXCLUDED.language_code,
    author_name      = EXCLUDED.author_name,
    author_uri       = EXCLUDED.author_uri,
    author_photo     = EXCLUDED.author_photo,
    published_at     = EXCLUDED.published_at,
    flag_content_uri = EXCLUDED.flag_content_uri,
    google_maps_uri  = EXCLUDED.google_maps_uri,
    raw              = EXCLUDED.raw
"""

CLEAR_PREVIEW_SQL = """
UPDATE photos SET is_preview = FALSE
WHERE place_id = $1 AND is_preview
"""

UPSERT_PHOTO_SQL = """
INSERT INTO photos (
    place_id, name, width_px, height_px,
    author_attributions, google_maps_uri, flag_content_uri, raw, is_preview
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (place_id, name) DO UPDATE SET
    width_px            = EXCLUDED.width_px,
    height_px           = EXCLUDED.height_px,
    author_attributions = EXCLUDED.author_attributions,
    google_maps_uri     = EXCLUDED.google_maps_uri,
    flag_content_uri    = EXCLUDED.flag_content_uri,
    raw                 = EXCLUDED.raw,
    is_preview          = EXCLUDED.is_preview
"""

ORDER_BY = Literal["rating", "location"]


async def init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: b"\x01" + orjson.dumps(v),
        decoder=lambda v: orjson.loads(v[1:]),
        schema="pg_catalog",
        format="binary",
    )


async def create_pool(
    min_size: int = 1, max_size: int = 10, command_timeout: float = 30.0,
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        PLACES_DB_URL,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        init=init_connection,
    )


async def find_places_rectangle(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int = 10,
    order_by: ORDER_BY = "rating",
    prefetch: int = 50) -> AsyncIterator[Place]:
    if location.south_west is None or location.north_east is None:
        raise ValueError("Location has no bounding box (south_west / north_east)")
    if order_by == "location" and (location.center_point is None):
        raise ValueError("Location has no center point")

    sw_lat, sw_lon = location.south_west
    ne_lat, ne_lon = location.north_east

    async with pool.acquire() as conn:
        async with conn.transaction():
            if order_by == "rating":
                cursor = conn.cursor(
                    QUERY_RECTANGLE_ORDER_BY_RATING,
                    main_type, sw_lon, sw_lat, ne_lon, ne_lat, max_results,
                    prefetch=prefetch,
                )
            else:
                center_lat, center_lon = location.center_point # type: ignore
                cursor = conn.cursor(
                    QUERY_RECTANGLE_ORDER_BY_LOCATION,
                    main_type, sw_lon, sw_lat, ne_lon, ne_lat,
                    center_lon, center_lat, max_results, # type: ignore
                    prefetch=prefetch,
                )
            async for row in cursor:
                yield Place(**add_preview_link_to_place(dict(row)))


async def find_places_circle(
    pool: asyncpg.Pool,
    location: Location,
    main_type: str,
    max_results: int = 10,
    order_by: ORDER_BY = "location",
    prefetch: int = 50) -> AsyncIterator[Place]:
    if location.center_point is None or location.radius is None:
        raise ValueError("Location has no center point / radius for circle search")

    center_lat, center_lon = location.center_point
    radius = location.radius
    query = QUERY_CIRCLE_ORDER_BY_RATING if order_by == "rating" else QUERY_CIRCLE_ORDER_BY_LOCATION

    async with pool.acquire() as conn:
        async with conn.transaction():
            async for row in conn.cursor(
                query, main_type, center_lon, center_lat, radius, max_results,
                prefetch=prefetch,
            ):
                yield Place(**add_preview_link_to_place(dict(row)))


async def fetch_place_detail(pool: asyncpg.Pool, place_id: str) -> PlaceDetail | None:
    async with pool.acquire() as conn:
        place_row = await conn.fetchrow(PLACE_QUERY, place_id)
        if place_row is None:
            return None
        review_rows = await conn.fetch(REVIEWS_QUERY, place_id)
        photo_rows = await conn.fetch(PHOTOS_QUERY, place_id)

    return PlaceDetail(
        **add_preview_link_to_place(dict(place_row)),
        reviews=[Review(**dict(row)) for row in review_rows],
        photos=[Photo(**add_url_to_photo(dict(row))) for row in photo_rows],
    )


async def fetch_reviews_for_ids(
    pool: asyncpg.Pool, place_ids: list[str], prefetch: int = 50,
) -> AsyncIterator[tuple[str, list[Review]]]:
    """Stream reviews grouped by place_id; relies on ORDER BY place_id in SQL."""
    if not place_ids:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            current_pid: str | None = None
            current_items: list[Review] = []
            async for row in conn.cursor(QUERY_FETCH_REVIEWS, place_ids, prefetch=prefetch):
                d = dict(row)
                pid = d.pop("place_id")
                if pid != current_pid:
                    if current_pid is not None:
                        yield current_pid, current_items
                    current_pid = pid
                    current_items = []
                current_items.append(Review(**d))
            if current_pid is not None:
                yield current_pid, current_items


async def fetch_photos_for_ids(
    pool: asyncpg.Pool, place_ids: list[str], prefetch: int = 50,
) -> AsyncIterator[tuple[str, list[Photo]]]:
    """Stream photos grouped by place_id; relies on ORDER BY place_id in SQL."""
    if not place_ids:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            current_pid: str | None = None
            current_items: list[Photo] = []
            async for row in conn.cursor(QUERY_FETCH_PHOTOS, place_ids, prefetch=prefetch):
                d = dict(row)
                pid = d.pop("place_id")
                if pid != current_pid:
                    if current_pid is not None:
                        yield current_pid, current_items
                    current_pid = pid
                    current_items = []
                current_items.append(Photo(**add_url_to_photo(d)))
            if current_pid is not None:
                yield current_pid, current_items


async def upsert_place(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    # conn: asyncpg.Connection,
    main_type: str,
    place_raw_json: dict[str, Any]) -> None:
    """Insert/update one place from google response"""
    location = place_raw_json.get("location", {})
    plus_code = place_raw_json.get("plusCode", {})
    display_name = place_raw_json.get("displayName", {})
    await conn.execute(
        UPSERT_PLACE_SQL,
        place_raw_json["id"],
        main_type,
        display_name.get("text"),
        place_raw_json.get("formattedAddress"),
        place_raw_json.get("internationalPhoneNumber"),
        place_raw_json.get("websiteUri"),
        place_raw_json.get("rating"),
        place_raw_json.get("userRatingCount"),
        location.get("longitude"),
        location.get("latitude"),
        plus_code.get("globalCode"),
        place_raw_json.get("types"),
        place_raw_json.get("regularOpeningHours"),
        place_raw_json.get("regularSecondaryOpeningHours"),
    )


async def upsert_reviews(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    # conn: asyncpg.Connection,
    place_id: str,
    reviews: list[dict[str, Any]] | None) -> None:
    """Insert new reviews"""
    if not reviews:
        return
    rows = [
        (
            place_id,
            review.get("name"),
            review.get("rating"),
            review.get("originalText", {}).get("text"),
            review.get("originalText", {}).get("languageCode"),
            review.get("authorAttribution", {}).get("displayName"),
            review.get("authorAttribution", {}).get("uri"),
            review.get("authorAttribution", {}).get("photoUri"),
            parse_published_at(review.get("publishTime")),
            review.get("flagContentUri"),
            review.get("googleMapsUri"),
            review, # store original raw JSONB data
        )
        for review in reviews
    ]
    await conn.executemany(UPSERT_REVIEW_SQL, rows)


async def upsert_photos(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    # conn: asyncpg.Connection,
    place_id: str,
    photos: list[dict[str, Any]] | None) -> None:
    """Insert new photos.
    The first photo in the batch is marked is_preview=TRUE"""
    if not photos:
        return
    rows = [
        (
            place_id,
            photo.get("name"),
            photo.get("widthPx"),
            photo.get("heightPx"),
            photo.get("authorAttributions"),
            photo.get("googleMapsUri"),
            photo.get("flagContentUri"),
            photo, # store original raw JSONB
            i == 0,
        )
        for i, photo in enumerate(photos)
    ]
    await conn.execute(CLEAR_PREVIEW_SQL, place_id)
    await conn.executemany(UPSERT_PHOTO_SQL, rows)


if __name__ == "__main__":
    async def _main() -> None:
        pool = await create_pool()
        try:
            CDMX_TEST = (19.412429, -99.1664120)

            rect_loc = Location.from_center_point(CDMX_TEST, 50_000, is_rectangle=True)
            circle_loc = Location.from_center_point(CDMX_TEST, 50_000, is_rectangle=False)
            rect_results = [p async for p in find_places_rectangle(pool, rect_loc, main_type="gym", max_results=2_000, order_by="rating")]
            circle_results = [p async for p in find_places_circle(pool, circle_loc, main_type="gym", max_results=2_000, order_by="location")]
            print(len(rect_results))
            print(len(circle_results))
            print("done")
        finally:
            await pool.close()

    asyncio.run(_main())
