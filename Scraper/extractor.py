"""
Extractor that runs two ticks in a loop. DB pg_places is requiered, will
function with one queue db down

Email tick:
    push: scan new (place_id, website) pairs and add them to queue
    pull: scan scraped emails from queue.success and add new to places.emails
Image tick:
    push: scan new photos where bucket_key is null and google_maps_uri
    is not null and add them to queue
    pull: scan scraped image bucket_key from success and update it in photos
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
from credentials import IMAGE_QUEUE_DB_URL, PLACES_DB_URL, QUEUE_DB_URL


BATCH_SIZE_DEFAULT = 1000

CONNECTION_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.InternalClientError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)

FETCH_EMAIL_CANDIDATES_SQL = """
SELECT place_id, website, fetched_at FROM places
WHERE website IS NOT NULL AND website <> ''
  AND fetched_at > $1
ORDER BY fetched_at
"""
FETCH_KNOWN_EMAIL_SQL = """
SELECT place_id, website FROM scrape_queue WHERE place_id = ANY($1)
UNION ALL SELECT place_id, website FROM success WHERE place_id = ANY($1)
UNION ALL SELECT place_id, website FROM error   WHERE place_id = ANY($1)
"""
INSERT_EMAIL_QUEUE_SQL = """
INSERT INTO scrape_queue (place_id, website) VALUES ($1, $2)
ON CONFLICT (place_id) DO UPDATE SET
    website = EXCLUDED.website,
    added_at = now(),
    attempts = 0
"""
READ_EMAIL_SCAN_WATERMARK_SQL = "SELECT last_scanned_at FROM extractor_state WHERE id = 1"
UPDATE_EMAIL_SCAN_WATERMARK_SQL = "UPDATE extractor_state SET last_scanned_at = $1 WHERE id = 1"
READ_EMAIL_SYNC_WATERMARK_SQL = "SELECT last_emails_synced_at FROM extractor_state WHERE id = 1"
UPDATE_EMAIL_SYNC_WATERMARK_SQL = "UPDATE extractor_state SET last_emails_synced_at = $1 WHERE id = 1"
FETCH_SUCCESS_EMAILS_SQL = """
SELECT place_id, emails, scraped_at FROM success
WHERE emails IS NOT NULL AND scraped_at > $1
ORDER BY scraped_at
"""
MERGE_EMAILS_SQL = """
UPDATE places
SET emails = ARRAY(
    SELECT DISTINCT unnest(COALESCE(emails, ARRAY[]::text[]) || $2::text[])
)
WHERE place_id = $1
"""

# first scrape preview images
# FETCH_IMAGE_CANDIDATES_SQL = """
# SELECT place_id, name AS photo_name, google_maps_uri
# FROM photos
# WHERE is_preview = TRUE
#   AND bucket_key IS NULL
#   AND google_maps_uri IS NOT NULL
#   AND google_maps_uri <> ''
# ORDER BY place_id, name
# """
FETCH_IMAGE_CANDIDATES_SQL = """
SELECT place_id, name AS photo_name, google_maps_uri
FROM photos
WHERE bucket_key IS NULL
  AND google_maps_uri IS NOT NULL
  AND google_maps_uri <> ''
ORDER BY place_id, name
"""
FETCH_KNOWN_IMAGE_SQL = """
SELECT place_id, photo_name FROM scrape_queue WHERE place_id = ANY($1)
UNION ALL SELECT place_id, photo_name FROM success WHERE place_id = ANY($1)
UNION ALL SELECT place_id, photo_name FROM error   WHERE place_id = ANY($1)
"""
INSERT_IMAGE_QUEUE_SQL = """
INSERT INTO scrape_queue (place_id, photo_name, google_maps_uri)
VALUES ($1, $2, $3)
ON CONFLICT (place_id, photo_name) DO NOTHING
"""
READ_IMAGE_SYNC_WATERMARK_SQL = "SELECT last_images_synced_at FROM extractor_state WHERE id = 1"
UPDATE_IMAGE_SYNC_WATERMARK_SQL = "UPDATE extractor_state SET last_images_synced_at = $1 WHERE id = 1"
FETCH_SUCCESS_IMAGES_SQL = """
SELECT place_id, photo_name, bucket_key, scraped_at FROM success
WHERE scraped_at > $1
ORDER BY scraped_at
"""
UPDATE_PHOTO_BUCKET_KEY_SQL = """
UPDATE photos SET bucket_key = $3
WHERE place_id = $1 AND name = $2
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


# email tick
async def insert_new_email_candidates(
    queue_pool: asyncpg.Pool, batch: list[asyncpg.Record]
    ) -> int:
    """Filter batch against pg_queue known set; INSERT new (place_id, website)"""
    place_ids = [row["place_id"] for row in batch]
    known_rows = await queue_pool.fetch(FETCH_KNOWN_EMAIL_SQL, place_ids)
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
            await conn.executemany(INSERT_EMAIL_QUEUE_SQL, to_insert)
    return len(to_insert)


async def merge_emails(places_pool: asyncpg.Pool, batch: list[asyncpg.Record]) -> int:
    """Update places.places.emails from queue.success.emails"""
    rows = [(r["place_id"], r["emails"]) for r in batch]
    if not rows:
        return 0
    async with places_pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(MERGE_EMAILS_SQL, rows)
    return len(rows)


async def email_push(
    places_pool: asyncpg.Pool, queue_pool: asyncpg.Pool, batch_size: int,
    ) -> None:
    last_scanned_at: datetime = await queue_pool.fetchval(READ_EMAIL_SCAN_WATERMARK_SQL)
    log.info(f"email push: last_scanned_at={last_scanned_at}")

    last_fetched_at: datetime | None = None
    total_scanned = 0
    total_inserted = 0

    async with places_pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(FETCH_EMAIL_CANDIDATES_SQL, last_scanned_at, prefetch=batch_size)
            buffer: list[asyncpg.Record] = []
            async for row in cursor:
                buffer.append(row)
                if len(buffer) >= batch_size:
                    total_inserted += await insert_new_email_candidates(queue_pool, buffer)
                    total_scanned += len(buffer)
                    last_fetched_at = buffer[-1]["fetched_at"]
                    log.info(f"  email push: scanned={total_scanned} inserted={total_inserted}")
                    buffer.clear()
            if buffer:
                total_inserted += await insert_new_email_candidates(queue_pool, buffer)
                total_scanned += len(buffer)
                last_fetched_at = buffer[-1]["fetched_at"]
                log.info(f"  email push: scanned={total_scanned} inserted={total_inserted}")

    # equals that extractor performed the task whether successfully or not
    if last_fetched_at is not None:
        await queue_pool.execute(UPDATE_EMAIL_SCAN_WATERMARK_SQL, last_fetched_at)
        log.info(f"email push: watermark -> {last_fetched_at}")
    log.info(f"email push summary: scanned={total_scanned} inserted={total_inserted}")


async def email_pull(
    places_pool: asyncpg.Pool, queue_pool: asyncpg.Pool, batch_size: int,
) -> None:
    last_synced_at: datetime = await queue_pool.fetchval(READ_EMAIL_SYNC_WATERMARK_SQL)
    log.info(f"email pull: last_emails_synced_at={last_synced_at}")

    last_scraped_at: datetime | None = None
    total_scanned = 0
    total_merged = 0

    async with queue_pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(FETCH_SUCCESS_EMAILS_SQL, last_synced_at, prefetch=batch_size)
            buffer: list[asyncpg.Record] = []
            async for row in cursor:
                buffer.append(row)
                if len(buffer) >= batch_size:
                    total_merged += await merge_emails(places_pool, buffer)
                    total_scanned += len(buffer)
                    last_scraped_at = buffer[-1]["scraped_at"]
                    log.info(f"  email pull: scanned={total_scanned} merged={total_merged}")
                    buffer.clear()
            if buffer:
                total_merged += await merge_emails(places_pool, buffer)
                total_scanned += len(buffer)
                last_scraped_at = buffer[-1]["scraped_at"]
                log.info(f"  email pull: scanned={total_scanned} merged={total_merged}")

    if last_scraped_at is not None:
        await queue_pool.execute(UPDATE_EMAIL_SYNC_WATERMARK_SQL, last_scraped_at)
        log.info(f"email pull: watermark -> {last_scraped_at}")
    log.info(f"email pull summary: scanned={total_scanned} merged={total_merged}")


# image tick
async def insert_new_image_candidates(
    image_queue_pool: asyncpg.Pool, batch: list[asyncpg.Record],
) -> int:
    """Filter batch against pg_image_queue known set; INSERT new (place_id, photo_name)"""
    place_ids = [row["place_id"] for row in batch]
    known_rows = await image_queue_pool.fetch(FETCH_KNOWN_IMAGE_SQL, place_ids)
    known: set[tuple[str, str]] = {(r["place_id"], r["photo_name"]) for r in known_rows}
    to_insert: list[tuple[str, str, str]] = []
    for row in batch:
        if (row["place_id"], row["photo_name"]) in known:
            continue
        to_insert.append((row["place_id"], row["photo_name"], row["google_maps_uri"]))
    if not to_insert:
        return 0
    async with image_queue_pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(INSERT_IMAGE_QUEUE_SQL, to_insert)
    return len(to_insert)


async def update_photo_bucket_keys(
    places_pool: asyncpg.Pool, batch: list[asyncpg.Record],
) -> int:
    rows = [(r["place_id"], r["photo_name"], r["bucket_key"]) for r in batch]
    if not rows:
        return 0
    async with places_pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPDATE_PHOTO_BUCKET_KEY_SQL, rows)
    return len(rows)


async def image_push(
    places_pool: asyncpg.Pool, image_queue_pool: asyncpg.Pool, batch_size: int,
) -> None:
    log.info("image push: scanning candidates")
    total_scanned = 0
    total_inserted = 0

    async with places_pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(FETCH_IMAGE_CANDIDATES_SQL, prefetch=batch_size)
            buffer: list[asyncpg.Record] = []
            async for row in cursor:
                buffer.append(row)
                if len(buffer) >= batch_size:
                    total_inserted += await insert_new_image_candidates(image_queue_pool, buffer)
                    total_scanned += len(buffer)
                    log.info(f"  image push: scanned={total_scanned} inserted={total_inserted}")
                    buffer.clear()
            if buffer:
                total_inserted += await insert_new_image_candidates(image_queue_pool, buffer)
                total_scanned += len(buffer)
                log.info(f"  image push: scanned={total_scanned} inserted={total_inserted}")

    log.info(f"image push summary: scanned={total_scanned} inserted={total_inserted}")


async def image_pull(
    places_pool: asyncpg.Pool, image_queue_pool: asyncpg.Pool, batch_size: int,
) -> None:
    last_synced_at: datetime = await image_queue_pool.fetchval(READ_IMAGE_SYNC_WATERMARK_SQL)
    log.info(f"image pull: last_images_synced_at={last_synced_at}")

    last_scraped_at: datetime | None = None
    total_scanned = 0
    total_updated = 0

    async with image_queue_pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(FETCH_SUCCESS_IMAGES_SQL, last_synced_at, prefetch=batch_size)
            buffer: list[asyncpg.Record] = []
            async for row in cursor:
                buffer.append(row)
                if len(buffer) >= batch_size:
                    total_updated += await update_photo_bucket_keys(places_pool, buffer)
                    total_scanned += len(buffer)
                    last_scraped_at = buffer[-1]["scraped_at"]
                    log.info(f"  image pull: scanned={total_scanned} updated={total_updated}")
                    buffer.clear()
            if buffer:
                total_updated += await update_photo_bucket_keys(places_pool, buffer)
                total_scanned += len(buffer)
                last_scraped_at = buffer[-1]["scraped_at"]
                log.info(f"  image pull: scanned={total_scanned} updated={total_updated}")

    if last_scraped_at is not None:
        await image_queue_pool.execute(UPDATE_IMAGE_SYNC_WATERMARK_SQL, last_scraped_at)
        log.info(f"image pull: watermark -> {last_scraped_at}")
    log.info(f"image pull summary: scanned={total_scanned} updated={total_updated}")


# ticks
async def try_create_pool(url: str, name: str) -> asyncpg.Pool | None:
    try:
        return await asyncpg.create_pool(url, min_size=1, max_size=2)
    except CONNECTION_ERRORS as e:
        log.warning(f"{name} pool create failed: {e!r}")
        return None


async def run_email_tick(
    places_pool: asyncpg.Pool, queue_pool: asyncpg.Pool | None, batch_size: int,
) -> None:
    if queue_pool is None:
        log.info("queue pool unavailable; email tick skipped")
        return
    try:
        await email_push(places_pool, queue_pool, batch_size)
        await email_pull(places_pool, queue_pool, batch_size)
    except CONNECTION_ERRORS as e:
        log.warning(f"email tick skipped: {e!r}")
    except Exception:
        log.exception("email tick failed")


async def run_image_tick(
    places_pool: asyncpg.Pool, image_queue_pool: asyncpg.Pool | None, batch_size: int,
) -> None:
    if image_queue_pool is None:
        log.info("image queue pool unavailable; image tick skipped")
        return
    try:
        await image_push(places_pool, image_queue_pool, batch_size)
        await image_pull(places_pool, image_queue_pool, batch_size)
    except CONNECTION_ERRORS as e:
        log.warning(f"image tick skipped: {e!r}")
    except Exception:
        log.exception("image tick failed")


async def run_service(batch_size: int, interval: int) -> None:
    """Loop both ticks every 'interval' seconds. Pools are created lazily
    so a DB that is down at startup or drops mid-run
    gets retried each cycle without losing the other tick."""
    main_task = asyncio.current_task()
    if not main_task:
        log.critical("main_task is None")
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)

    log.info(f"Service started, interval={interval}s, batch_size={batch_size}")
    places_pool: asyncpg.Pool | None = None
    queue_pool: asyncpg.Pool | None = None
    image_queue_pool: asyncpg.Pool | None = None
    try:
        while True:
            if places_pool is None:
                places_pool = await try_create_pool(PLACES_DB_URL, "places")
            if queue_pool is None:
                queue_pool = await try_create_pool(QUEUE_DB_URL, "queue")
            if image_queue_pool is None:
                image_queue_pool = await try_create_pool(IMAGE_QUEUE_DB_URL, "image_queue")

            if places_pool is None:
                log.warning("places pool unavailable; skipping all ticks this cycle")
            else:
                await run_email_tick(places_pool, queue_pool, batch_size)
                await run_image_tick(places_pool, image_queue_pool, batch_size)

            await asyncio.sleep(float(interval))
    except asyncio.CancelledError:  # NOSONAR
        log.info("Shutdown clean")
    finally:
        for pool in (places_pool, queue_pool, image_queue_pool):
            if pool is not None:
                await pool.close()


async def main(batch_size: int, interval: int | None) -> None:
    """One-shot run if interval is None, otherwise long-running service."""
    if interval is None:
        # One-shot mode: terminate() (sync, immediate) instead of close() to
        # avoid a multi-minute hang when the DB has gone away with connections
        # still checked out.
        places_pool: asyncpg.Pool | None = None
        queue_pool: asyncpg.Pool | None = None
        image_queue_pool: asyncpg.Pool | None = None
        try:
            places_pool = await asyncpg.create_pool(PLACES_DB_URL, min_size=1, max_size=2)
            queue_pool = await asyncpg.create_pool(QUEUE_DB_URL, min_size=1, max_size=2)
            image_queue_pool = await asyncpg.create_pool(IMAGE_QUEUE_DB_URL, min_size=1, max_size=2)
            await run_email_tick(places_pool, queue_pool, batch_size)
            await run_image_tick(places_pool, image_queue_pool, batch_size)
        finally:
            for pool in (places_pool, queue_pool, image_queue_pool):
                if pool is not None:
                    pool.terminate()
    else:
        await run_service(batch_size, interval)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    def bounded[T: (int, float)](t: type[T], low: T, high: T) -> Callable[[str], T]:
        """argparse type: parse with type(), then enforce low <= value <= high"""
        def check(s: str) -> T:
            try:
                v = t(s)
            except ValueError:
                raise argparse.ArgumentTypeError(f"expected {t.__name__}, got {s!r}")
            if not (low <= v <= high):
                raise argparse.ArgumentTypeError(f"must be in [{low}, {high}], got {v}")
            return v
        return check

    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", default=BATCH_SIZE_DEFAULT,
                        type=bounded(int, 1, 1_000_000),
                        help=f"batch size, default {BATCH_SIZE_DEFAULT}")
    parser.add_argument("--interval", default=None,
                        type=bounded(int, 1, 86_400),
                        help="poll interval in seconds. If omitted, run once and exit.")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.batch_size, args.interval))
    except KeyboardInterrupt:
        log.info("Terminating")
    except CONNECTION_ERRORS:
        log.exception("DB unavailable")
