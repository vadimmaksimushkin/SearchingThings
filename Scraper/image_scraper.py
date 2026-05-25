"""Long-running image scraper service.

Drains pg_image_queue.scrape_queue one job at a time: claim a row, navigate
to the photo's google_maps_uri, capture the first lh3.googleusercontent.com
s1024-v1 response, convert it to WebP, upload to Cloudflare R2, and record
the outcome in success/error + attempt_log.

Single sequential worker. Browser + context are recycled together every N
in [10, 15] jobs to refresh the playwright session and rotate UA.
"""
# FIXME: simplify wrappers and handlers
# FIXME: remove bugs with error handling and DB reconnection
import argparse
import asyncio
import asyncpg
import aioboto3 # pyright: ignore[reportMissingTypeStubs]
import sys
import logging
import random
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from PIL import Image
from playwright.async_api import (
    Playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
    Route,
    TimeoutError as PWTimeout,
    async_playwright
)
from playwright_stealth import Stealth  # pyright: ignore[reportMissingTypeStubs]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import (
    IMAGE_QUEUE_DB_URL,
    R2_ACCESS_KEY,
    R2_ACCOUNT_ID,
    R2_BUCKET,
    R2_SECRET_ACCESS_KEY,
)

MAX_ATTEMPTS = 3
LOCK_DURATION = timedelta(minutes=5.0)
POLL_INTERVAL_S = 1.0
PAGE_TIMEOUT_MS = 15_000
CAPTURE_TIMEOUT_S = 12.0
JITTER_RANGE_S = (1.0, 2.0) # 2 times lower then planned
SETTLE_RANGE_S = (0.5, 1.0) # 2 times lowe then planned
BODY_CAP_BYTES = 10_000_000  # ~10MB safety cap on captured response body
RECYCLE_RANGE = (10, 15)    # close+relaunch browser+context+playwright every N jobs

LH3_PREFIX = "https://lh3.googleusercontent.com"
LH3_SUFFIX = "s1024-v1"

R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

# Neutral modern Chrome UAs (Linux + macOS). No email, no "scraping" tokens.
USER_AGENTS = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
)

CONNECTION_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.InternalClientError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_URL_SUBSTRINGS = ("/gen_204", "/log", "/csi", "/ping")
BLOCKED_HOST_PREFIXES = ("khms", "mt0.googleapis", "mt1.googleapis", "mt2.googleapis", "mt3.googleapis")


CLAIM_SQL = """
UPDATE scrape_queue
SET locked_until = now() + $1,
    attempts = attempts + 1,
    last_attempt_at = now()
WHERE id = (
    SELECT id FROM scrape_queue
    WHERE locked_until IS NULL OR locked_until < now()
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, place_id, photo_name, google_maps_uri, attempts
"""
LOG_START_SQL = """
INSERT INTO attempt_log (place_id, photo_name, attempt_no)
VALUES ($1, $2, $3)
RETURNING log_id
"""
LOG_FINISH_SQL = """
UPDATE attempt_log
SET finished_at = now(), outcome = $2, reason = $3
WHERE log_id = $1
"""
INSERT_SUCCESS_SQL = """
INSERT INTO success (place_id, photo_name, bucket_key, webp_bytes, attempts)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (place_id, photo_name) DO NOTHING
"""
INSERT_ERROR_SQL = """
INSERT INTO error (place_id, photo_name, attempts, reason)
VALUES ($1, $2, $3, $4)
ON CONFLICT (place_id, photo_name) DO NOTHING
"""
DELETE_QUEUE_RECORD_SQL = """
DELETE FROM scrape_queue WHERE place_id = $1 AND photo_name = $2
"""
UNLOCK_QUEUE_RECORD_SQL = """
UPDATE scrape_queue SET locked_until = NULL WHERE place_id = $1 AND photo_name = $2
"""
# Unlock AND roll back the attempt if worker is interrupted by shutdown
UNLOCK_AND_DECREMENT_SQL = """
UPDATE scrape_queue
SET locked_until = NULL,
    attempts = GREATEST(attempts - 1, 0)
WHERE place_id = $1 AND photo_name = $2
"""

SHUTDOWN_REASON = "shutdown_interrupted"

log = logging.getLogger(__name__)


@dataclass
class ClaimedJob:
    id: int
    log_id: int
    place_id: str
    photo_name: str
    google_maps_uri: str
    attempts: int


@dataclass
class ScrapeResult:
    success: bool
    bucket_key: str = ""
    webp_bytes: int = 0
    reason: str = "ok"


def bucket_key_for(place_id: str, photo_name: str) -> str:
    """photos/{place_id}/{LONG_REF}.webp"""
    long_ref = photo_name.rsplit("/", 1)[-1]
    return f"photos/{place_id}/{long_ref}.webp"


async def block_assets(route: Route) -> None:
    """Block: images/media/fonts, map tiles.
    Always allow lh3.googleusercontent.com — that's the target."""
    request = route.request
    url = request.url
    if url.startswith(LH3_PREFIX):
        await route.continue_()
        return
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return
    if any(s in url for s in BLOCKED_URL_SUBSTRINGS):
        await route.abort()
        return
    host = request.url.split("/", 3)[2] if "://" in request.url else ""
    if any(host.startswith(p) for p in BLOCKED_HOST_PREFIXES):
        await route.abort()
        return
    await route.continue_()


async def human_mouse_move(page: Page, duration_s: float) -> None:
    """Settle on the page with a few small mouse hops over duration_s"""
    steps = random.randint(5, 10)
    x = random.randint(300, 1100)
    y = random.randint(200, 600)
    per_step = duration_s / steps
    for _ in range(steps):
        x = max(20, min(1340, x + random.randint(-60, 60)))
        y = max(20, min(740, y + random.randint(-40, 40)))
        try:
            await page.mouse.move(x, y, steps=random.randint(3, 8))
        except Exception:
            return
        await asyncio.sleep(per_step)


def encode_webp(body: bytes) -> bytes:
    """Pillow is CPU-blocking, run on a worker thread via asyncio.to_thread"""
    with Image.open(BytesIO(body)) as opened:
        src = opened if opened.mode in ("RGB", "RGBA") else opened.convert("RGB")
        try:
            out = BytesIO()
            src.save(out, "webp", quality=90, method=6)
            return out.getvalue()
        finally:
            if src is not opened:
                src.close()


async def launch_browser_context(
    playwright_inst: Playwright, headless: bool,
) -> tuple[Browser, BrowserContext]:
    """Fresh chromium + new context with a random UA and asset blocking attached."""
    browser = await playwright_inst.chromium.launch(headless=headless)
    user_agent = random.choice(USER_AGENTS)
    context = await browser.new_context(
        user_agent=user_agent,
        locale="en-US",
        viewport={"width": 1366, "height": 768},
    )
    await context.route("**/*", block_assets)
    log.info(f"browser launched headless={headless} ua={user_agent.rsplit(' ', 1)[-1]}")
    return browser, context


async def scrape_one_photo(context: BrowserContext, google_maps_uri: str) -> tuple[bytes | None, str]:
    """Open one tab, get the first lh3 s1024-v1 body, settle, close tab.
    Returns (body_bytes_or_none, reason)"""
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)

    captured: dict[str, Response] = {}
    capture_event = asyncio.Event()

    def on_response(resp: Response) -> None:
        if captured:
            return
        url = resp.url
        if url.startswith(LH3_PREFIX) and url.endswith(LH3_SUFFIX):
            captured["response"] = resp
            capture_event.set()

    page.on("response", on_response)

    try:
        try:
            await page.goto(google_maps_uri, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        except PWTimeout:
            # goto timeout is not fatal — the response may have already fired.
            log.warning("page.goto timeout; checking for prior capture")
        except Exception as e:
            return None, f"goto_failed: {e!r}"

        if not capture_event.is_set():
            try:
                await asyncio.wait_for(capture_event.wait(), timeout=CAPTURE_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None, "no_lh3_match_within_timeout"

        resp = captured["response"]
        try:
            body = await resp.body()
        except Exception as e:
            return None, f"body_read_failed: {e!r}"
        if len(body) > BODY_CAP_BYTES:
            return None, f"body_too_large: {len(body)} bytes"

        await human_mouse_move(page, random.uniform(*SETTLE_RANGE_S))
        return body, "ok"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def claim_and_log_start(pool: asyncpg.Pool) -> ClaimedJob | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(CLAIM_SQL, LOCK_DURATION)
            if not row:
                return None
            log_id = await conn.fetchval(
                LOG_START_SQL, row["place_id"], row["photo_name"], row["attempts"],
            )
            return ClaimedJob(
                id=row["id"],
                log_id=log_id,
                place_id=row["place_id"],
                photo_name=row["photo_name"],
                google_maps_uri=row["google_maps_uri"],
                attempts=row["attempts"],
            )


# async def record_outcome_deprecated(pool: asyncpg.Pool, job: ClaimedJob, result: ScrapeResult) -> None:
#     """Move job to success/error, unlock for retry, or unlock-and-decrement on
#     shutdown interrupt; update the attempt_log row"""
#     async with pool.acquire() as conn:
#         async with conn.transaction():
#             if result.reason == SHUTDOWN_REASON:
#                 await conn.execute(LOG_FINISH_SQL, job.log_id, "interrupted", result.reason)
#                 await conn.execute(UNLOCK_AND_DECREMENT_SQL, job.place_id, job.photo_name)
#                 return
#             outcome = "success" if result.success else "error"
#             await conn.execute(LOG_FINISH_SQL, job.log_id, outcome, result.reason)
#             if result.success:
#                 await conn.execute(DELETE_QUEUE_RECORD_SQL, job.place_id, job.photo_name)
#                 await conn.execute(
#                     INSERT_SUCCESS_SQL,
#                     job.place_id, job.photo_name,
#                     result.bucket_key, result.webp_bytes, job.attempts,
#                 )
#             elif job.attempts >= MAX_ATTEMPTS:
#                 await conn.execute(DELETE_QUEUE_RECORD_SQL, job.place_id, job.photo_name)
#                 await conn.execute(
#                     INSERT_ERROR_SQL,
#                     job.place_id, job.photo_name, job.attempts, result.reason,
#                 )
#             else:
#                 await conn.execute(UNLOCK_QUEUE_RECORD_SQL, job.place_id, job.photo_name)


async def sleep_or_shutdown(shutdown_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def open_pool_with_retry(
    poll_interval_s: float, shutdown_event: asyncio.Event,
) -> asyncpg.Pool | None:
    while not shutdown_event.is_set():
        try:
            return await asyncpg.create_pool(IMAGE_QUEUE_DB_URL, min_size=1, max_size=2)
        except CONNECTION_ERRORS as e:
            log.warning(f"DB unavailable: {e!r}; retrying in {poll_interval_s}s")
            if await sleep_or_shutdown(shutdown_event, poll_interval_s):
                return None
    return None


async def process_one_job(
    job: ClaimedJob,
    context: BrowserContext,
    bucket,
) -> ScrapeResult:
    """End-to-end pipeline for one claimed job: jitter, scrape, encode, upload."""
    await asyncio.sleep(random.uniform(*JITTER_RANGE_S))

    body, reason = await scrape_one_photo(context, job.google_maps_uri)
    if body is None:
        return ScrapeResult(success=False, reason=reason)

    try:
        webp = await asyncio.to_thread(encode_webp, body)
    except Exception as e:
        return ScrapeResult(success=False, reason=f"webp_encode_failed: {e!r}")

    key = bucket_key_for(job.place_id, job.photo_name)
    try:
        await bucket.put_object(
            Bucket=R2_BUCKET, Key=key, Body=webp, ContentType="image/webp",
        )
    except Exception as e:
        return ScrapeResult(success=False, reason=f"r2_put_failed: {e!r}")

    return ScrapeResult(success=True, bucket_key=key, webp_bytes=len(webp), reason="ok")


async def claim_with_retry(
    pool: asyncpg.Pool, shutdown_event: asyncio.Event, poll_interval_s: float,
) -> ClaimedJob | None:
    """Claim one job. On DB error, empty queue, or unexpected exception, sleep
    poll_interval_s (or until shutdown) and return None. Caller just loops back."""
    job: ClaimedJob | None = None
    try:
        job = await claim_and_log_start(pool)
    except CONNECTION_ERRORS as e:
        log.warning(f"DB unavailable: {e!r}; retrying in {poll_interval_s}s")
    except Exception:
        log.exception("claim failed")
    if job is None:
        await sleep_or_shutdown(shutdown_event, poll_interval_s)
    return job


async def record_outcome(pool: asyncpg.Pool, job: ClaimedJob, result: ScrapeResult) -> None:
    """Move job to success/error, unlock for retry, or unlock-and-decrement on
    shutdown interrupt; update the attempt_log row"""
    try:
        # await record_outcome_deprecated(pool, job, result)
        async with pool.acquire() as conn:
            async with conn.transaction():
                if result.reason == SHUTDOWN_REASON:
                    await conn.execute(LOG_FINISH_SQL, job.log_id, "interrupted", result.reason)
                    await conn.execute(UNLOCK_AND_DECREMENT_SQL, job.place_id, job.photo_name)
                    return
                outcome = "success" if result.success else "error"
                await conn.execute(LOG_FINISH_SQL, job.log_id, outcome, result.reason)
                if result.success:
                    await conn.execute(DELETE_QUEUE_RECORD_SQL, job.place_id, job.photo_name)
                    await conn.execute(
                        INSERT_SUCCESS_SQL,
                        job.place_id, job.photo_name,
                        result.bucket_key, result.webp_bytes, job.attempts,
                    )
                elif job.attempts >= MAX_ATTEMPTS:
                    await conn.execute(DELETE_QUEUE_RECORD_SQL, job.place_id, job.photo_name)
                    await conn.execute(
                        INSERT_ERROR_SQL,
                        job.place_id, job.photo_name, job.attempts, result.reason,
                    )
                else:
                    await conn.execute(UNLOCK_QUEUE_RECORD_SQL, job.place_id, job.photo_name)
    except CONNECTION_ERRORS as e:
        log.warning(f"DB unavailable recording {job.place_id}: {e!r}")
    except Exception:
        log.exception(f"record_outcome failed for {job.place_id}")


def log_outcome(job: ClaimedJob, result: ScrapeResult) -> None:
    if result.success:
        log.info(f"success place={job.place_id} bytes={result.webp_bytes} key={result.bucket_key}")
    elif result.reason == SHUTDOWN_REASON:
        log.info(f"interrupted place={job.place_id} (unlocked, will retry on restart)")
    elif job.attempts >= MAX_ATTEMPTS:
        log.info(f"terminal place={job.place_id} reason={result.reason}")
    else:
        log.info(f"retry place={job.place_id} attempt={job.attempts} reason={result.reason}")


async def recycle_browser(
    p: Playwright, browser: Browser, context: BrowserContext, headless: bool,
):
    """Close browser+context AND restart the Playwright driver.
    Playwright recycle plugs memory leak"""
    log.info("recycling browser + playwright driver")
    try:
        await context.close()
        await browser.close()
    except Exception:
        log.exception("error during browser close")
    try:
        await p.stop()
    except Exception:
        log.exception("error during playwright stop")
    new_p = await async_playwright().start()
    new_browser, new_context = await launch_browser_context(new_p, headless)
    return new_p, new_browser, new_context, random.randint(*RECYCLE_RANGE)


async def handle_one_job(
    job: ClaimedJob,
    context: BrowserContext,
    s3,
    pool: asyncpg.Pool,
    shutdown_event: asyncio.Event,
) -> None:
    """Process one claimed job end-to-end: run pipeline, reclassify on shutdown,
    record outcome, log it. Caller's loop just calls this and increments a counter"""
    log.info(
        f"claim place={job.place_id} "
        f"photo={job.photo_name.rsplit('/', 1)[-1]} "
        f"attempt={job.attempts}"
    )
    try:
        result = await process_one_job(job, context, s3)
    except Exception as e:
        result = ScrapeResult(success=False, reason=f"unhandled: {e!r}")

    # If shutdown was requested mid-job, chromium was likely killed by
    # terminal-propagated SIGINT — the failure isn't real. Reclassify so we
    # don't burn an attempt or log it as a retry.
    if not result.success and shutdown_event.is_set():
        result = ScrapeResult(success=False, reason=SHUTDOWN_REASON)

    await record_outcome(pool, job, result)
    log_outcome(job, result)


async def drain_close(closers: list[tuple[Callable | None, str]]) -> None:
    """Best-effort close-in-order on shutdown. Driver-already-gone is normal
    during Ctrl+C, so one-line note instead of a traceback."""
    for closer, label in closers:
        if closer is None:
            continue
        try:
            await closer()
        except Exception as e:
            log.info(f"{label} close skipped: {e!r}")


def install_shutdown_handler(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def on_shutdown() -> None:
        if not shutdown_event.is_set():
            log.info("Shutdown signal received, draining in-flight job...")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_shutdown)


async def run_service(headless: bool, poll_interval_s: float = POLL_INTERVAL_S) -> None:
    """Single-worker loop: claim → scrape → upload → record. Recycles browser
    every N in RECYCLE_RANGE jobs. SIGTERM/SIGINT drains the current job first."""
    shutdown_event = asyncio.Event()
    install_shutdown_handler(shutdown_event)

    pool = await open_pool_with_retry(poll_interval_s, shutdown_event)
    if pool is None:
        log.info("Shutdown clean")
        return

    # Manual playwright start/stop so we can suppress cleanup errors when the
    # chromium subprocess is killed by SIGINT propagation from the terminal —
    # the async-with form would re-raise them as the wrapping context unwinds.
    session = aioboto3.Session()
    async with session.client( # pyright: ignore
        "R2",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    ) as bucket: # pyright: ignore
        p = await async_playwright().start()
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            browser, context = await launch_browser_context(p, headless)
            jobs_until_recycle = random.randint(*RECYCLE_RANGE)
            jobs_done_this_cycle = 0
            log.info(f"Service started, recycle after {jobs_until_recycle} jobs")

            while not shutdown_event.is_set():
                job = await claim_with_retry(pool, shutdown_event, poll_interval_s)
                if job is None:
                    continue

                await handle_one_job(job, context, bucket, pool, shutdown_event)

                jobs_done_this_cycle += 1
                if jobs_done_this_cycle >= jobs_until_recycle:
                    p, browser, context, jobs_until_recycle = await recycle_browser(
                        p, browser, context, headless,
                    )
                    jobs_done_this_cycle = 0
        finally:
            await drain_close([
                (context.close if context else None, "context"),
                (browser.close if browser else None, "browser"),
                (p.stop, "playwright"),
                (pool.close, "pool"),
            ])

    log.info("Shutdown clean")


def bounded[T: (int, float)](t: type[T], low: T, high: T) -> Callable[[str], T]:
    def check(s: str) -> T:
        try:
            v = t(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected {t.__name__}, got {s!r}")
        if not (low <= v <= high):
            raise argparse.ArgumentTypeError(f"must be in [{low}, {high}], got {v}")
        return v
    return check


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    p = argparse.ArgumentParser()
    p.add_argument("--no-headless", action="store_true",
                   help="run chromium with a visible window (calibration only)")
    p.add_argument("--poll-interval", default=POLL_INTERVAL_S,
                   type=bounded(float, 0.01, 3_600.0),
                   help=f"seconds to sleep when queue is empty (default {POLL_INTERVAL_S})")
    args = p.parse_args()

    try:
        asyncio.run(run_service(headless=not args.no_headless, poll_interval_s=args.poll_interval))
    except KeyboardInterrupt:
        log.info("Terminating")
    except CONNECTION_ERRORS:
        log.exception("DB unavailable")
    except Exception:
        log.exception("unhandled exception during shutdown")
