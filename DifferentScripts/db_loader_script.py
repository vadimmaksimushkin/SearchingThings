"""One-time bootstrap loader.

Reads malls_5193.json + gyms_no_email.json and inserts the entries into the
main DB (places, reviews, photos). Idempotent via ON CONFLICT DO NOTHING:
safe to re-run.
"""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import PLACES_DB_URL

MALLS_PATH = Path("malls_5193.json")
GYMS_PATH = Path("gyms_no_email.json")

BATCH = 500

# Google's publishTime has nanoseconds; Python's fromisoformat handles up to
# microseconds. Trim extra digits.
NANO_TRIM = re.compile(r"(\.\d{6})\d+")


def parse_published_at(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(NANO_TRIM.sub(r"\1", s))


def place_row(entry: dict[str, Any], main_type: str) -> tuple[Any, ...]:
    coords = entry.get("coordinates") or {}
    # Normalize "no emails" to NULL (drop empty lists).
    email = entry.get("email") or None
    return (
        entry["place_id"],
        main_type,
        entry.get("name"),
        entry.get("address"),
        entry.get("phone"),
        entry.get("website"),
        entry.get("rating"),
        entry.get("rating_count"),
        coords.get("longitude"),
        coords.get("latitude"),
        entry.get("plus_code"),
        entry.get("category"),
        entry.get("opening_hours"),
        entry.get("secondary_opening_hours"),
        email,
    )


def review_rows(entry: dict[str, Any]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    place_id = entry["place_id"]
    for r in entry.get("reviews") or []:
        original = r.get("originalText") or {}
        author = r.get("authorAttribution") or {}
        rows.append((
            place_id,
            r.get("name"),
            r.get("rating"),
            original.get("text"),
            original.get("languageCode"),
            author.get("displayName"),
            author.get("uri"),
            author.get("photoUri"),
            parse_published_at(r.get("publishTime")),
            r.get("flagContentUri"),
            r.get("googleMapsUri"),
            r,
        ))
    return rows


def photo_rows(entry: dict[str, Any]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    place_id = entry["place_id"]
    for p in entry.get("photos") or []:
        rows.append((
            place_id,
            p.get("name"),
            p.get("widthPx"),
            p.get("heightPx"),
            p.get("authorAttributions"),
            p.get("googleMapsUri"),
            p.get("flagContentUri"),
            p,
        ))
    return rows


PLACE_INSERT = """
INSERT INTO places (
    place_id, main_type, name, address, phone, website,
    rating, rating_count, geog, plus_code, category,
    opening_hours, secondary_opening_hours, emails
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8,
    ST_SetSRID(ST_MakePoint($9, $10), 4326)::geography,
    $11, $12, $13, $14, $15
)
ON CONFLICT (place_id) DO NOTHING
"""

REVIEW_INSERT = """
INSERT INTO reviews (
    place_id, name, rating, text, language_code,
    author_name, author_uri, author_photo,
    published_at, flag_content_uri, google_maps_uri, raw
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
ON CONFLICT (place_id, name) DO NOTHING
"""

PHOTO_INSERT = """
INSERT INTO photos (
    place_id, name, width_px, height_px,
    author_attributions, google_maps_uri, flag_content_uri, raw
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
ON CONFLICT (place_id, name) DO NOTHING
"""


async def chunked_executemany(
    conn: asyncpg.Connection,
    sql: str,
    rows: list[tuple[Any, ...]],
    label: str,
) -> None:
    total = len(rows)
    if total == 0:
        return
    for i in range(0, total, BATCH):
        chunk = rows[i : i + BATCH]
        await conn.executemany(sql, chunk)
        print(f"  {label}: {min(i + BATCH, total)}/{total}", file=sys.stderr)


async def load_file(conn: asyncpg.Connection, path: Path, main_type: str) -> None:
    print(f"Loading {path} as main_type={main_type!r}", file=sys.stderr)
    with open(path, encoding="utf-8") as f:
        data: list[dict[str, Any]] = json.load(f)
    print(f"  read {len(data)} entries from JSON", file=sys.stderr)

    places: list[tuple[Any, ...]] = []
    reviews: list[tuple[Any, ...]] = []
    photos: list[tuple[Any, ...]] = []
    skipped = 0
    for entry in data:
        if not entry.get("place_id"):
            skipped += 1
            continue
        places.append(place_row(entry, main_type))
        reviews.extend(review_rows(entry))
        photos.extend(photo_rows(entry))
    if skipped:
        print(f"  skipped {skipped} entries with no place_id", file=sys.stderr)

    async with conn.transaction():
        await chunked_executemany(conn, PLACE_INSERT, places, "places")
        await chunked_executemany(conn, REVIEW_INSERT, reviews, "reviews")
        await chunked_executemany(conn, PHOTO_INSERT, photos, "photos")


async def main() -> None:
    conn = await asyncpg.connect(PLACES_DB_URL)
    try:
        # Let asyncpg auto-encode dicts/lists as JSONB.
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        for path, main_type in [
            (MALLS_PATH, "shopping_mall"),
            (GYMS_PATH, "gym"),
        ]:
            if not path.exists():
                print(f"skip: {path} not found", file=sys.stderr)
                continue
            await load_file(conn, path, main_type)

        print(file=sys.stderr)
        for tbl in ("places", "reviews", "photos"):
            n = await conn.fetchval(f"SELECT count(*) FROM {tbl}")
            print(f"DB total: {tbl} = {n}", file=sys.stderr)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
