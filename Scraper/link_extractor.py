"""
Link extractor that streams places in batches from the main DB with
fetched_at > last_scanned_at and pushes them to scrape_queue if not already
present in scrape_queue, success, or error. last_scanned_at keeps each run
bounded to places added since the previous successful run.
"""
import asyncio
import logging
import signal
import sys
import asyncpg
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import PLACES_DB_URL, QUEUE_DB_URL

BATCH_SIZE_DEFAULT = 1000

FETCH_CANDIDATES_SQL = """
SELECT place_id, website, fetched_at FROM places
WHERE website IS NOT NULL AND website <> ''
  AND fetched_at > $1
ORDER BY fetched_at
"""
FETCH_KNOWN_SQL = """
SELECT place_id, website FROM scrape_queue WHERE place_id = ANY($1)
UNION ALL SELECT place_id, website FROM success WHERE place_id = ANY($1)
UNION ALL SELECT place_id, website FROM error   WHERE place_id = ANY($1)
"""
INSERT_SQL = """
INSERT INTO scrape_queue (place_id, website) VALUES ($1, $2)
ON CONFLICT (place_id) DO UPDATE SET
    website = EXCLUDED.website,
    added_at = now(),
    attempts = 0
"""
READ_EXTRACTOR_TIMESTAMP_SQL = """
SELECT last_scanned_at FROM link_extractor_state WHERE id = 1
"""
UPDATE_EXTRACTOR_TIMESTAMP_SQL = """
UPDATE link_extractor_state SET last_scanned_at = $1 WHERE id = 1
"""
log = logging.getLogger(__name__)


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
    """Push a candidate (place_id, website) in batch to scrape_queue when that exact pair
    is not already known in scrape_queue/success/error. ON CONFLICT on scrape_queue updates
    the website if the place is pending with a stale URL. Returns inserted/updated count"""
    place_ids = [row["place_id"] for row in batch]
    known_rows = await queue_pool.fetch(FETCH_KNOWN_SQL, place_ids)
    known: set[tuple[str, str]] = {(row["place_id"], row["website"]) for row in known_rows}
    to_insert: list[tuple[str, str]] = []

    for row in batch:
        website = normalize_website(row["website"])
        if not website or ((row["place_id"], website) in known):
            continue
        to_insert.append((row["place_id"], website))

    if not to_insert:
        return 0

    async with queue_pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(INSERT_SQL, to_insert)
    return len(to_insert)


async def run_tick(
    places_pool: asyncpg.Pool,
    queue_pool: asyncpg.Pool,
    batch_size: int) -> None:
    """One full extraction pass: read last_scanned_at, scan candidates, bump last_scanned_at"""
    last_scanned_at: datetime = await queue_pool.fetchval(READ_EXTRACTOR_TIMESTAMP_SQL)
    log.info(f"last_scanned_at={last_scanned_at}")

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
                    log.info(f"  scanned={total_scanned} inserted={total_inserted}")
                    buffer.clear()
            if buffer:
                total_inserted += await insert_new(queue_pool, buffer)
                total_scanned += len(buffer)
                last_fetched_at = buffer[-1]["fetched_at"]
                log.info(f"  scanned={total_scanned} inserted={total_inserted}")


    #equals that link extractor performed the task whether successfully or not
    if last_fetched_at is not None:
        await queue_pool.execute(UPDATE_EXTRACTOR_TIMESTAMP_SQL, last_fetched_at)
        log.info(f"Timestamp bumped to {last_fetched_at}")
    else:
        log.info("No new candidates")

    log.info(f"Summary: scanned={total_scanned} inserted={total_inserted}")


async def run_service(
    places_pool: asyncpg.Pool,
    queue_pool: asyncpg.Pool,
    batch_size: int,
    interval: int) -> None:
    """Loop run_tick every 'interval' seconds until SIGTERM/SIGINT"""
    main_task = asyncio.current_task()
    if not main_task:
        log.critical("main_task is None")
        return None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)

    log.info(f"Service started, interval={interval}s")
    while True:
        try:
            await run_tick(places_pool, queue_pool, batch_size)
            await asyncio.sleep(float(interval))
        except Exception as e:
            log.exception("tick failed ", e)
        except asyncio.CancelledError:
            break
    log.info("Shutdown clean")


async def main(batch_size: int, interval: int | None) -> None:
    """Runs one time if interval is None, works like service otherwise"""
    async with asyncpg.create_pool(PLACES_DB_URL, min_size=1, max_size=2) as places_pool:
        async with asyncpg.create_pool(QUEUE_DB_URL, min_size=1, max_size=2) as queue_pool:
            if interval is None:
                await run_tick(places_pool, queue_pool, batch_size)
            else:
                await run_service(places_pool, queue_pool, batch_size, interval)


#FIXME: Database is down exception
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    def _bounded[T: (int, float)](type: type[T], low: T, high: T) -> Callable[[str], T]:
        """argparse type: parse with type(), then enforce low <= value <= high"""
        def check(input_string: str) -> T:
            try:
                value = type(input_string)
            except ValueError:
                raise argparse.ArgumentTypeError(f"expected {type.__name__}, got {input_string!r}")
            if not (low <= value <= high):
                raise argparse.ArgumentTypeError(f"must be in [{low}, {high}], got {value}")
            return value
        return check

    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--batch-size", default=BATCH_SIZE_DEFAULT,
        type=_bounded(int, 1, 1_000_000),
        help=f"Specity the batch size, default {BATCH_SIZE_DEFAULT}")
    argument_parser.add_argument("--interval", default=None,
        type=_bounded(int, 1, 86_400), # 60*60*24
        help="Poll interval in seconds. If omitted, run once and exit.")
    args = argument_parser.parse_args()
    batch_size = args.batch_size
    interval = args.interval

    asyncio.run(main(batch_size, interval))