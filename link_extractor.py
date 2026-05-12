"""
Link extractor that streams places in batches from the main DB with
fetched_at > last_scanned_at and pushes them to scrape_queue if not already
present in scrape_queue, success, or error. last_scanned_at keeps each run
bounded to places added since the previous successful run.
"""
import asyncio
import sys
import asyncpg
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from api_key import PLACES_DB_URL, QUEUE_DB_URL


BATCH_SIZE_DEFAULT = 1000

FETCH_CANDIDATES_SQL = """
SELECT place_id, website, fetched_at FROM places
WHERE website IS NOT NULL AND website <> ''
  AND fetched_at > $1
ORDER BY fetched_at
"""
FETCH_KNOWN_SQL = """
SELECT place_id FROM scrape_queue WHERE place_id = ANY($1)
UNION SELECT place_id FROM success WHERE place_id = ANY($1)
UNION SELECT place_id FROM error   WHERE place_id = ANY($1)
"""
INSERT_SQL = """
INSERT INTO scrape_queue (place_id, website) VALUES ($1, $2)
ON CONFLICT (place_id) DO NOTHING
"""
READ_EXTRACTOR_TIMESTAMP_SQL = """
SELECT last_scanned_at FROM link_extractor_state WHERE id = 1
"""
UPDATE_EXTRACTOR_TIMESTAMP_SQL = """
UPDATE link_extractor_state SET last_scanned_at = $1 WHERE id = 1
"""


def normalize_website(url: str) -> str | None:
    """Add scheme if missing, lowercase host. Path/query/fragment untouched."""
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path, p.params, p.query, p.fragment))


async def insert_new(queue_pool: asyncpg.Pool, batch: list[asyncpg.Record]) -> int:
    """Push a candidate (place_id, website) in batch to scrape_queue that are not present
    in queue DB, return inserted count"""
    place_ids = [row["place_id"] for row in batch]
    known_rows = await queue_pool.fetch(FETCH_KNOWN_SQL, place_ids)
    known = {row["place_id"] for row in known_rows}
    to_insert: list[tuple[str, str]] = []

    for row in batch:
        if row["place_id"] in known:
            continue
        website = normalize_website(row["website"])
        if website:
            to_insert.append((row["place_id"], website))

    if not to_insert:
        return 0

    async with queue_pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(INSERT_SQL, to_insert)
    return len(to_insert)


async def extract_pending_links_batch(
    places_pool: asyncpg.Pool,
    queue_pool: asyncpg.Pool,
    last_scanned_at: datetime,
    batch_size: int = BATCH_SIZE_DEFAULT,
    ) -> tuple[datetime | None, int, int]:
    """Stream candidates with fetched_at > last_scanned_at, filter known IDs, push new to queue"""
    last_fetched_at: datetime | None = None
    total_scanned = 0
    total_inserted = 0

    async with places_pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(FETCH_CANDIDATES_SQL, last_scanned_at, prefetch=batch_size)
            buffer: list[asyncpg.Record] = []
            async for row in cursor:
                buffer.append(row)
                if len(buffer) >= batch_size:
                    total_inserted += await insert_new(queue_pool, buffer)
                    total_scanned += len(buffer)
                    last_fetched_at = buffer[-1]["fetched_at"]
                    print(f"  scanned={total_scanned} inserted={total_inserted}", file=sys.stderr)
                    buffer.clear()
            if buffer:
                total_inserted += await insert_new(queue_pool, buffer)
                total_scanned += len(buffer)
                last_fetched_at = buffer[-1]["fetched_at"]
                print(f"  scanned={total_scanned} inserted={total_inserted}", file=sys.stderr)

    return last_fetched_at, total_scanned, total_inserted


async def main(batch_size: int = BATCH_SIZE_DEFAULT) -> None:
    """Check last_scanned_at, push new candidates."""
    async with asyncpg.create_pool(PLACES_DB_URL, min_size=1, max_size=2) as places_pool:
        async with asyncpg.create_pool(QUEUE_DB_URL, min_size=1, max_size=2) as queue_pool:
            last_scanned_at: datetime = await queue_pool.fetchval(READ_EXTRACTOR_TIMESTAMP_SQL)
            print(f"last_scanned_at={last_scanned_at}", file=sys.stderr)

            last_fetched_at, total_scanned, total_inserted = await extract_pending_links_batch(
                places_pool, queue_pool, last_scanned_at, batch_size
            )

            #equals that link extractor performed the task whether successfully or not
            if last_fetched_at is not None:
                await queue_pool.execute(UPDATE_EXTRACTOR_TIMESTAMP_SQL, last_fetched_at)
                print(f"Timestamp bumped to {last_fetched_at}", file=sys.stderr)
            else:
                print("no new candidates", file=sys.stderr)

            print(f"Summary: scanned={total_scanned} inserted={total_inserted}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--batch-size", default=BATCH_SIZE_DEFAULT, type=int)
    args = argument_parser.parse_args()
    batch_size = args.batch_size

    asyncio.run(main(batch_size))