"""Long-running email scraper service.

Drains scrape_queue: each worker atomically claims a row, scrapes the site
for emails, then writes the outcome to the success or error table and
finalizes the attempt_log audit row. Connects only to the queue DB.
"""
import asyncio
import html as html_lib
import re
import sys
import asyncpg
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from playwright.async_api import Browser, Page, Route, TimeoutError as PWTimeout, async_playwright
from playwright_stealth import Stealth  # pyright: ignore[reportMissingTypeStubs]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import QUEUE_DB_URL
from constants import ASSET_EXTS, CONTACT_KEYWORDS

WORKER_COUNT = 3
MAX_ATTEMPTS = 3
LOCK_DURATION = timedelta(minutes=5.0)
POLL_INTERVAL_S = 1.0
PAGE_TIMEOUT_MS = 10_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

CONNECTION_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.InternalClientError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)

CONTACT_RE = re.compile(
    r"(?i)(?:^|[/\-_?#=])("
    + "|".join(re.escape(k) for k in CONTACT_KEYWORDS)
    + r")(?:$|[/\-_?#&.])"
)

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_URL_EXTS = {"pdf", "zip", "exe", "dmg", "tar", "gz"}


async def _block_heavy(route: Route) -> None:
    """Abort image/media/font requests and known binary downloads to keep
    renderer memory bounded. CSS and JS pass through so layout/JS-rendered
    text is still available to the scraper."""
    request = route.request
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return
    path = urlparse(request.url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if ext in BLOCKED_URL_EXTS:
        await route.abort()
        return
    await route.continue_()

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
RETURNING id, place_id, website, attempts
"""

LOG_START_SQL = """
INSERT INTO attempt_log (place_id, attempt_no, website)
VALUES ($1, $2, $3)
RETURNING log_id
"""

LOG_FINISH_SQL = """
UPDATE attempt_log
SET finished_at = now(), outcome = $2, reason = $3
WHERE log_id = $1
"""

INSERT_SUCCESS_SQL = """
INSERT INTO success (place_id, website, emails, final_website, attempts)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (place_id, website) DO NOTHING
"""

INSERT_ERROR_SQL = """
INSERT INTO error (place_id, website, attempts, reason)
VALUES ($1, $2, $3, $4)
ON CONFLICT (place_id, website) DO NOTHING
"""
DELETE_QUEUE_RECORD_SQL = """
DELETE FROM scrape_queue WHERE place_id = $1
"""
UNLOCK_QUEUE_RECORD_SQL = """
UPDATE scrape_queue SET locked_until = NULL WHERE place_id = $1
"""
log = logging.getLogger(__name__)


@dataclass
class ClaimedJob:
    id: int
    log_id: int
    place_id: str
    website: str
    attempts: int


@dataclass
class ScrapeResult:
    success: bool
    emails: list[str] = field(default_factory=list[str])
    final_website: str = ""
    reason: str = "ok"


async def scroll_to_bottom(page: Page, max_steps: int = 20, step_pause_ms: int = 300) -> None:
    last_height = 0
    for _ in range(max_steps):
        height: int = await page.evaluate("""
            Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight,
                document.body.offsetHeight,
                document.documentElement.offsetHeight
            )
        """)
        if height == last_height:
            break
        await page.evaluate(f"window.scrollTo(0, {height})")
        await page.wait_for_timeout(step_pause_ms)
        last_height = height


# FIXME: better return
async def _goto(page: Page, url: str) -> bool:
    """page.goto() wrapper with error handling"""
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except PWTimeout:
        return False
    except Exception as e:
        # print(f"  goto error on {url}: {e}", file=sys.stderr)
        log.error(f"  goto error on {url}: {e}")
        return False
    try:
        await scroll_to_bottom(page)
    except Exception as e:
        # print(f"  scroll error on {url}: {e}", file=sys.stderr)
        log.error(f"  scroll error on {url}: {e}")
    return True


def normalize_url(url: str) -> str:
    """Strip query and fragments of a given URL"""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


async def get_contact_urls(page: Page) -> set[str]:
    """Scan page for URLs and collect ones with contact keywords in them.
    Skips hrefs whose path ends in a known binary/asset extension so we
    never spend a navigation on a PDF, image, or stylesheet"""
    urls = await page.locator("a[href]").all()
    contact_urls: set[str] = set()
    for url in urls:
        href = await url.get_attribute("href")
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        href = urljoin(page.url, href)
        path = urlparse(href).path.lower()
        ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
        if ext in ASSET_EXTS:
            continue
        if CONTACT_RE.search(href):
            contact_urls.add(normalize_url(href))
    return contact_urls


def is_asset_or_sentry(email: str) -> bool:
    """Basic filter for assets like 'asset-name@100x100px.jpg' or
    '8eb368c655b84e029ed79ad7a5c1718e@sentry.wixpress.com' sentries"""
    domain = email.rpartition("@")[2].lower()
    top_level_domain = domain.rsplit(".", 1)[-1]
    return (top_level_domain in ASSET_EXTS) or ("sentry" in domain)


async def get_emails(page: Page) -> set[str]:
    """load html and return set of email by regex"""
    html_decoded = html_lib.unescape(await page.content())
    return {e for e in EMAIL_RE.findall(html_decoded) if not is_asset_or_sentry(e)}


async def scrape_one_site(browser: Browser, website: str) -> ScrapeResult:
    """Crawl one website and its contact/about URLs"""
    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/Mexico_City",
        viewport={"width": 1366, "height": 768},
    )
    await context.route("**/*", _block_heavy)
    try:
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        if not await _goto(page, website):
            return ScrapeResult(
                success=False,
                final_website=website,
                reason="main_page_load_failed")

        final_website = page.url
        emails = await get_emails(page)
        contact_urls: set[str] = set()
        try:
            contact_urls = await get_contact_urls(page)
        except Exception as e:
            log.warning(f"  contact URL discovery failed on {website}: {e!r}")

        for contact_url in contact_urls:
            try:
                if await _goto(page, contact_url):
                    emails.update(await get_emails(page))
            except Exception as e:
                log.warning(f"  contact page failed {contact_url}: {e!r}")



        return ScrapeResult(
            success=True,
            emails=sorted(emails),
            final_website=final_website,
            reason="ok")
    finally:
        await context.close() # executes before any return anyways


async def claim_and_log_start(pool: asyncpg.Pool) -> ClaimedJob | None:
    """Set lock, bump attemtps, bump last_attempt_at on scrape_queue and insert
    a draft attempt_log"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # lock, bump attempts, return (id, place_id, website_attempts)
            row = await conn.fetchrow(CLAIM_SQL, LOCK_DURATION)
            if not row:
                return None
            # insert (place_id, attempt_no, website) return log_id
            log_id = await conn.fetchval(
                LOG_START_SQL, row["place_id"], row["attempts"], row["website"]
            )
            return ClaimedJob(
                id=row["id"],
                log_id=log_id,
                place_id=row["place_id"],
                website=row["website"],
                attempts=row["attempts"],
            )


async def record_outcome(pool: asyncpg.Pool, job: ClaimedJob, result: ScrapeResult) -> None:
    """Update DB on outcome, update attempt log, transfer record from queue to success
    or error, or unlock it for future use"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            outcome = "success" if result.success else "error"
            await conn.execute(LOG_FINISH_SQL, job.log_id, outcome, result.reason)
            if result.success:
                await conn.execute(DELETE_QUEUE_RECORD_SQL, job.place_id)
                await conn.execute(
                    INSERT_SUCCESS_SQL,
                    job.place_id,
                    job.website,
                    result.emails or None,
                    result.final_website,
                    job.attempts
                )
            elif job.attempts >= MAX_ATTEMPTS:
                await conn.execute(DELETE_QUEUE_RECORD_SQL, job.place_id)
                await conn.execute(
                    INSERT_ERROR_SQL,
                    job.place_id,
                    job.website,
                    job.attempts,
                    result.reason,
                )
            elif (not result.success) and job.attempts < MAX_ATTEMPTS:
                await conn.execute(UNLOCK_QUEUE_RECORD_SQL, job.place_id)
            else:
                log.critical(f"ELSE STATEMENT REACHED {job} {result}")


async def _sleep_or_shutdown(shutdown_event: asyncio.Event, seconds: float) -> bool:
    """Sleep up to 'seconds' or return early if shutdown is signaled.
    Returns True when shutdown is signaled, False on timeout."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def worker_loop(
    worker_id: int,
    browser: Browser,
    pool: asyncpg.Pool,
    shutdown_event: asyncio.Event,
    poll_interval_s: float = POLL_INTERVAL_S,
    max_attempts: int = MAX_ATTEMPTS) -> None:
    """Claim, scrape, record. The shutdown check sits only between jobs:
    once a job is claimed, scrape_one_site and record_outcome always run to
    completion so the in-flight URL is never abandoned with the queue row
    still locked."""
    while not shutdown_event.is_set():
        try:
            job = await claim_and_log_start(pool)
        except CONNECTION_ERRORS as e:
            log.warning(f"w{worker_id} DB unavailable: {e!r}; retrying in {poll_interval_s}s")
            if await _sleep_or_shutdown(shutdown_event, poll_interval_s):
                break
            continue
        except Exception:
            log.exception(f"w{worker_id} claim failed")
            if await _sleep_or_shutdown(shutdown_event, poll_interval_s):
                break
            continue
        if job is None:
            if await _sleep_or_shutdown(shutdown_event, poll_interval_s):
                break
            continue

        log.info(f"w{worker_id} claim {job.place_id} attempt={job.attempts}")
        try:
            result = await scrape_one_site(browser, job.website)
        except Exception as e:
            result = ScrapeResult(success=False, final_website=job.website, reason=repr(e))

        try:
            await record_outcome(pool, job, result)
        except CONNECTION_ERRORS as e:
            log.warning(f"w{worker_id} DB unavailable recording {job.place_id}: {e!r}")
            continue
        except Exception:
            log.exception(f"w{worker_id} record_outcome failed for {job.place_id}")
            continue

        if result.success:
            log.info(f"w{worker_id} success {job.place_id} emails={len(result.emails)}")
        elif job.attempts >= max_attempts:
            log.info(f"w{worker_id} terminal {job.place_id} reason={result.reason}")
        else:
            log.info(f"w{worker_id} retry {job.place_id} attempt={job.attempts} reason={result.reason}")
    log.info(f"w{worker_id} exit")


async def _open_pool_with_retry(
    worker_count: int,
    poll_interval_s: float,
    shutdown_event: asyncio.Event) -> asyncpg.Pool | None:
    """Create the queue pool, retrying every poll_interval_s if DB is unreachable.
    Returns None if shutdown was signaled before a pool could be opened."""
    while not shutdown_event.is_set():
        try:
            return await asyncpg.create_pool(
                QUEUE_DB_URL, min_size=2, max_size=worker_count + 2)
        except CONNECTION_ERRORS as e:
            log.warning(f"DB unavailable: {e!r}; retrying in {poll_interval_s}s")
            if await _sleep_or_shutdown(shutdown_event, poll_interval_s):
                return None
    return None


async def run_service(
    browser: Browser,
    worker_count: int = WORKER_COUNT,
    poll_interval_s: float = POLL_INTERVAL_S,
    max_attempts: int = MAX_ATTEMPTS) -> None:
    """Run workers until SIGTERM/SIGINT. Signal sets a shutdown event that
    workers check between jobs; in-flight scrapes complete before exit.
    Pool is created lazily so DB-down-at-startup gets the same retry treatment."""
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_shutdown() -> None:
        if not shutdown_event.is_set():
            log.info("Shutdown signal received, draining workers...")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_shutdown)

    log.info(f"Service started, workers={worker_count}")
    pool = await _open_pool_with_retry(worker_count, poll_interval_s, shutdown_event)
    if pool is None:
        log.info("Shutdown clean")
        return
    try:
        await asyncio.gather(
            *(worker_loop(i, browser, pool, shutdown_event, poll_interval_s, max_attempts)
              for i in range(worker_count))
        )
    finally:
        await pool.close()
    log.info("Shutdown clean")


async def main(
    worker_count: int = WORKER_COUNT,
    max_attempts: int = MAX_ATTEMPTS,
    poll_interval_s: float = POLL_INTERVAL_S) -> None:
    """Launch browser, run service until signal"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        await run_service(browser, worker_count, poll_interval_s, max_attempts)


# FIXME: Cap the page size download and memory usage or worker
# FIXME: Handle 403, denied, facebook login page and other page blockers
# FIXME: Potentially handle cookie banner
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

    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--workers", default=WORKER_COUNT, type=bounded(int, 1, 512),
        help=f"INT     Specity the amount of parallel workers, default {WORKER_COUNT}")
    argument_parser.add_argument("--max-attempts", default=MAX_ATTEMPTS, type=bounded(int, 1, 32),
        help=f"INT     Specity the max attempts per job, default {MAX_ATTEMPTS}")
    argument_parser.add_argument("--poll-interval", default=POLL_INTERVAL_S, type=bounded(float, 0.01, 3_600.0),
        help=f"FLOAT   Poll interval of each worker in seconds, default {POLL_INTERVAL_S}")
    args = argument_parser.parse_args()
    worker_count = args.workers
    max_attempts = args.max_attempts
    poll_interval_s = args.poll_interval

    try:
        asyncio.run(main(worker_count, max_attempts, poll_interval_s))
    except KeyboardInterrupt:
        log.info("Terminating")
