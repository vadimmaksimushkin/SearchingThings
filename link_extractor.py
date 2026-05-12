"""
Link extractor that looks up places in main DB and insert them into scraper's
scrape_queue if they are not already present here or in success and error tables
"""
# FIXME: rework not to store large data in RAM
import asyncio
import sys
import asyncpg
from urllib.parse import urlparse, urlunparse

from api_key import PLACES_DB_URL, QUEUE_DB_URL


BATCH_SIZE_DEFAULT = 1000
INSERT_SQL = """
INSERT INTO scrape_queue (place_id, website) VALUES ($1, $2)
ON CONFLICT (place_id) DO NOTHING
"""
FETCH_FROM_MAIN_SQL = """
SELECT place_id, website FROM places
WHERE website IS NOT NULL AND website != ''
"""
FETCH_PLACE_ID_FROM_QUEUE_SQL = """
SELECT place_id FROM scrape_queue
UNION SELECT place_id FROM success
UNION SELECT place_id FROM error
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


async def fetch_candidates(pool: asyncpg.Pool) -> dict[str, str]:
    """Returns a dictionary with {place_id, website} keys from main database"""
    rows = await pool.fetch(FETCH_FROM_MAIN_SQL)
    candidates: dict[str, str] = {}
    for row in rows:
        place_id = row["place_id"]
        website = normalize_website(row["website"])
        if website:
            candidates[place_id] = website
    return candidates


async def fetch_known(pool: asyncpg.Pool) -> set[str]:
    """Returns set of place_id from queue database"""
    rows = await pool.fetch(FETCH_PLACE_ID_FROM_QUEUE_SQL)
    return {r["place_id"] for r in rows}


async def enqueue(pool: asyncpg.Pool, rows: list[tuple[str, str]], batch_size: int = BATCH_SIZE_DEFAULT) -> None:
    """Pushes rows of (place_id, website) into scrape_queue"""
    total = len(rows)
    if total == 0:
        return
    async with pool.acquire() as conn:
        for i in range(0, total, batch_size):
            chunk = rows[i : i + batch_size]
            async with conn.transaction():
                await conn.executemany(INSERT_SQL, chunk)
            print(f"  enqueued: {i + len(chunk)}/{total}", file=sys.stderr)


async def enqueue_pending(
    places_pool: asyncpg.Pool,
    queue_pool: asyncpg.Pool,
    batch_size: int = BATCH_SIZE_DEFAULT) -> None:
    """Fetches place_id, already scraped place_id and pushes new to queue DB"""
    candidates = await fetch_candidates(places_pool)
    known = await fetch_known(queue_pool)
    new_ids: set[str] = candidates.keys() - known
    rows_to_insert = [(place_id, candidates[place_id]) for place_id in new_ids]

    print(f"candidates={len(candidates)} already_known={len(known)}",
          f"to_enqueue={len(rows_to_insert)}", file=sys.stderr)
    await enqueue(queue_pool, rows_to_insert, batch_size)


async def main(batch_size: int = BATCH_SIZE_DEFAULT) -> None:
    """Main function that creates connection pools, extracts and pushes links"""
    async with asyncpg.create_pool(PLACES_DB_URL, min_size=1, max_size=2) as places_pool:
        async with asyncpg.create_pool(QUEUE_DB_URL, min_size=1, max_size=2) as queue_pool:
            await enqueue_pending(places_pool, queue_pool, batch_size)


if __name__ == "__main__":
    import argparse
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--batch-size", default=BATCH_SIZE_DEFAULT, type=int)
    args = argument_parser.parse_args()
    batch_size = args.batch_size

    asyncio.run(main(batch_size))
