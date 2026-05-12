"""One-shot link extractor.

Reads places from the main DB and inserts their websites into the scraper's
scrape_queue for any place not already present in scrape_queue, success, or
error. Purely additive: errored places stay in error, successful places stay
in success. Re-runnable.
"""
import asyncio
import sys
from urllib.parse import urlparse, urlunparse

import asyncpg

from api_key import PLACES_DB_URL, QUEUE_DB_URL

BATCH = 1000


def normalize_website(url: str) -> str:
    """Add scheme if missing, lowercase host. Path/query/fragment untouched."""
    url = url.strip()
    if not url:
        return url
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path, p.params, p.query, p.fragment))


async def fetch_candidates(pool: asyncpg.Pool) -> dict[str, str]:
    rows = await pool.fetch(
        "SELECT place_id, website FROM places "
        "WHERE website IS NOT NULL AND website != ''"
    )
    return {r["place_id"]: normalize_website(r["website"]) for r in rows}


async def fetch_known(pool: asyncpg.Pool) -> set[str]:
    rows = await pool.fetch(
        "SELECT place_id FROM scrape_queue "
        "UNION SELECT place_id FROM success "
        "UNION SELECT place_id FROM error"
    )
    return {r["place_id"] for r in rows}


INSERT_SQL = """
INSERT INTO scrape_queue (place_id, website) VALUES ($1, $2)
ON CONFLICT (place_id) DO NOTHING
"""


async def enqueue(pool: asyncpg.Pool, rows: list[tuple[str, str]]) -> None:
    total = len(rows)
    if total == 0:
        return
    async with pool.acquire() as conn:
        for i in range(0, total, BATCH):
            chunk = rows[i : i + BATCH]
            async with conn.transaction():
                await conn.executemany(INSERT_SQL, chunk)
            print(f"  enqueued: {min(i + BATCH, total)}/{total}", file=sys.stderr)


async def enqueue_pending(
    places_pool: asyncpg.Pool,
    queue_pool: asyncpg.Pool,
) -> dict[str, int]:
    candidates = await fetch_candidates(places_pool)
    known = await fetch_known(queue_pool)
    new_ids = candidates.keys() - known
    to_insert = [(pid, candidates[pid]) for pid in new_ids]

    print(
        f"candidates={len(candidates)} "
        f"already_known={len(known)} "
        f"to_enqueue={len(to_insert)}",
        file=sys.stderr,
    )
    await enqueue(queue_pool, to_insert)

    return {
        "candidates": len(candidates),
        "already_known": len(known),
        "newly_queued": len(to_insert),
    }


async def main() -> None:
    places_pool = await asyncpg.create_pool(PLACES_DB_URL, min_size=1, max_size=2)
    queue_pool = await asyncpg.create_pool(QUEUE_DB_URL, min_size=1, max_size=2)
    try:
        result = await enqueue_pending(places_pool, queue_pool)
        print(file=sys.stderr)
        print(f"Summary: {result}", file=sys.stderr)
    finally:
        await places_pool.close()
        await queue_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
