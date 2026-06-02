"""
Scraper in active rework. Crawls websites, collects internal links
and adds them to FIFO scraping queue (max cap 100 internal links).
Finds emails and uploads page html to R2 bucket. Later they will
be parsed using a separated parser to find information categories
like `description`, `services`, `catalog` and others
"""
# FIXME: Add better headers, persistent context and better UA rotation
# FIXME: handle sitemap and robots.txt
import asyncio
import html as html_lib
import re
import sys
import random
import asyncpg
import aioboto3  # pyright: ignore[reportMissingTypeStubs]
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    Route,
    TimeoutError as PWTimeout,
    async_playwright,
)
from playwright_stealth import Stealth  # pyright: ignore[reportMissingTypeStubs]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import (
    QUEUE_DB_URL,
    R2_ACCOUNT_ID,
    R2_PAGES_ACCESS_KEY,
    R2_PAGES_BUCKET,
    R2_PAGES_SECRET_ACCESS_KEY,
)
from constants import ASSET_EXTS

WORKER_COUNT = 3
MAX_ATTEMPTS = 3
LOCK_DURATION = timedelta(minutes=5.0)
POLL_INTERVAL_S = 1.0
PAGE_TIMEOUT_MS = 15_000
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

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
BLOCKED_URL_EXTS = {"pdf", "zip", "exe", "dmg", "tar", "gz"}
SHUTDOWN_REASON = "shutdown_interrupted"

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
RETURNING id, place_id, site_domain, page_uri, attempts
"""

LOG_START_SQL = """
INSERT INTO attempt_log (place_id, site_domain, page_uri, attempt_no)
VALUES ($1, $2, $3, $4)
RETURNING log_id
"""

LOG_FINISH_SQL = """
UPDATE attempt_log
SET finished_at = now(), outcome = $2, reason = $3
WHERE log_id = $1
"""

INSERT_SUCCESS_SQL = """
INSERT INTO success
    (place_id, site_domain, page_uri, final_uri, http_status, r2_key, bytes, emails, attempts)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (place_id, site_domain, page_uri) DO NOTHING
"""

INSERT_ERROR_SQL = """
INSERT INTO error (place_id, site_domain, page_uri, http_status, attempts, reason)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (place_id, site_domain, page_uri) DO NOTHING
"""

DELETE_QUEUE_RECORD_SQL = """
DELETE FROM scrape_queue WHERE place_id = $1 AND site_domain = $2 AND page_uri = $3
"""

UNLOCK_QUEUE_RECORD_SQL = """
UPDATE scrape_queue SET locked_until = NULL
WHERE place_id = $1 AND site_domain = $2 AND page_uri = $3
"""

# Unlock AND roll back the attempt when a worker is interrupted by shutdown
UNLOCK_AND_DECREMENT_SQL = """
UPDATE scrape_queue
SET locked_until = NULL,
    attempts = GREATEST(attempts - 1, 0)
WHERE place_id = $1 AND site_domain = $2 AND page_uri = $3
"""

LOCK_PAGE_CAP_SQL = """
SELECT pages_remaining FROM place_uri
WHERE place_id = $1 AND site_domain = $2
FOR UPDATE
"""

FETCH_KNOWN_PAGES_SQL = """
SELECT page_uri FROM scrape_queue WHERE place_id = $1 AND site_domain = $2
UNION ALL SELECT page_uri FROM success WHERE place_id = $1 AND site_domain = $2
UNION ALL SELECT page_uri FROM error   WHERE place_id = $1 AND site_domain = $2
"""

INSERT_CHILD_SQL = """
INSERT INTO scrape_queue (place_id, site_domain, page_uri)
VALUES ($1, $2, $3)
ON CONFLICT (place_id, site_domain, page_uri) DO NOTHING
"""

DECREMENT_BUDGET_SQL = """
UPDATE place_uri SET pages_remaining = GREATEST(pages_remaining - $3, 0)
WHERE place_id = $1 AND site_domain = $2
"""

log = logging.getLogger(__name__)


@dataclass
class ClaimedJob:
    id: int
    log_id: int
    place_id: str
    site_domain: str
    page_uri: str
    attempts: int


@dataclass
class ScrapeResult:
    success: bool
    emails: list[str] = field(default_factory=list) # pyright: ignore[reportUnknownVariableType]
    links: set[str] = field(default_factory=set) # pyright: ignore[reportUnknownVariableType]
    final_uri: str = ""
    http_status: int | None = None
    r2_key: str = ""
    bytes: int = 0
    reason: str = "ok"


async def block_heavy_assets(route: Route) -> None:
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


def normalize_url(url: str) -> str:
    """Strip query and fragments of a given URL"""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


def normalize_host_path(url: str) -> str:
    """
    Scheme- and www-insensitive 'host+path' used only for the same-site testing
    """
    p = urlparse(url)
    host = (p.hostname or "")
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    return f"{host}{path}".lower()


def is_same_site(site_domain: str, url: str) -> bool:
    base = normalize_host_path(site_domain)
    target = normalize_host_path(url)
    return target == base or target.startswith(base + "/")


def page_r2_key(place_id: str, final_uri: str) -> str:
    p = urlparse(final_uri)
    rest = f"{p.hostname or ''}{p.path}".rstrip("/")
    return f"pages/{place_id}/{rest}.html"


def is_asset_or_sentry(email: str) -> bool:
    """Basic filter for assets like 'asset-name@100x100px.jpg' or
    '8eb368c655b84e029ed79ad7a5c1718e@sentry.wixpress.com' sentries"""
    domain = email.rpartition("@")[2].lower()
    top_level_domain = domain.rsplit(".", 1)[-1]
    return (top_level_domain in ASSET_EXTS) or ("sentry" in domain)


def emails_from_html(html: str) -> set[str]:
    """Return emails by regex in html page"""
    decoded = html_lib.unescape(html)
    return {e for e in EMAIL_RE.findall(decoded) if not is_asset_or_sentry(e)}


async def discover_links(page: Page, site_domain: str) -> set[str]:
    """Collect same-site page URLs. Skips assets and the root itself"""
    base = normalize_host_path(site_domain)
    out: set[str] = set()
    for anchor in await page.locator("a[href]").all():
        href = await anchor.get_attribute("href")
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(page.url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        path = parsed.path.lower()
        ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
        if ext in ASSET_EXTS:
            continue
        target = normalize_host_path(absolute)
        if target == base or not target.startswith(base + "/"):
            continue  # the root (handled as "") or off-site
        out.add(normalize_url(absolute))
    return out


async def process_one_job(
    browser: Browser,
    bucket: aioboto3.Session,
    job: ClaimedJob
) -> ScrapeResult:
    """Scrape one page: get final URL, scroll, regex emails, upload HTML to R2,
    same-site links. Has fresh context per page"""
    target = job.page_uri or job.site_domain
    context: BrowserContext = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        locale="en-US",
        timezone_id="America/Mexico_City",
        viewport={"width": 1366, "height": 768},
    )
    await context.route("**/*", block_heavy_assets)
    try:
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            response: Response | None = await page.goto(
                target, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
            )
        except PWTimeout:
            return ScrapeResult(success=False, final_uri=target, reason="page_load_timeout")
        except Exception as e:
            return ScrapeResult(success=False, final_uri=target, reason=f"goto_failed: {e!r}")

        status = response.status if response else None
        final_uri = page.url
        if status is None or status >= 400:
            return ScrapeResult(
                success=False, http_status=status, final_uri=final_uri,
                reason=f"http_status_{status}",
            )

        try:
            await scroll_to_bottom(page)
        except Exception as e:
            log.warning(f"  scroll failed on {final_uri}: {e!r}")

        html = await page.content()
        emails = emails_from_html(html)
        try:
            links = await discover_links(page, job.site_domain)
        except Exception as e:
            log.warning(f"  link discovery failed on {final_uri}: {e!r}")
            links: set[str] = set()

        body = html.encode("utf-8", "replace")
        key = page_r2_key(job.place_id, final_uri)
        try:
            await bucket.put_object( # type: ignore
                Bucket=R2_PAGES_BUCKET, Key=key, Body=body,
                ContentType="text/html; charset=utf-8",
            )
        except Exception as e:
            return ScrapeResult(
                success=False, http_status=status, final_uri=final_uri,
                reason=f"r2_put_failed: {e!r}",
            )

        return ScrapeResult(
            success=True,
            emails=sorted(emails),
            links=links,
            final_uri=final_uri,
            http_status=status,
            r2_key=key,
            bytes=len(body),
            reason="ok",
        )
    finally:
        await context.close()


async def claim_and_log_start(pool: asyncpg.Pool) -> ClaimedJob | None:
    """Set lock, bump attemtps, bump last_attempt_at on scrape_queue and insert
    a draft attempt_log"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(CLAIM_SQL, LOCK_DURATION)
            if not row:
                return None
            log_id = await conn.fetchval(
                LOG_START_SQL, row["place_id"], row["site_domain"],
                row["page_uri"], row["attempts"],
            )
            return ClaimedJob(
                id=row["id"],
                log_id=log_id,
                place_id=row["place_id"],
                site_domain=row["site_domain"],
                page_uri=row["page_uri"],
                attempts=row["attempts"],
            )


async def enqueue_children(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    job: ClaimedJob,
    links: set[str],
) -> int:
    """Filter discovered links against the known links, cap at the remainings,
    insert and decrement the budget by the number enqueued.
    Returns inserted count."""
    remaining: int | None = await conn.fetchval(LOCK_PAGE_CAP_SQL, job.place_id, job.site_domain)
    if not remaining or not links:
        return 0
    known_rows = await conn.fetch(FETCH_KNOWN_PAGES_SQL, job.place_id, job.site_domain)
    known = {r["page_uri"] for r in known_rows}
    to_insert = [u for u in sorted(links) if u not in known][:remaining]
    if not to_insert:
        return 0
    await conn.executemany(
        INSERT_CHILD_SQL, [(job.place_id, job.site_domain, u) for u in to_insert]
    )
    await conn.execute(DECREMENT_BUDGET_SQL, job.place_id, job.site_domain, len(to_insert))
    return len(to_insert)


async def record_outcome(
    pool: asyncpg.Pool, job: ClaimedJob, result: ScrapeResult, max_attempts: int,
) -> None:
    """Update DB on outcome, update attempt log, transfer record from queue
    to success or error, or unlock it for future use or unlock and decrement
    on shutdown interruption"""
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if result.reason == SHUTDOWN_REASON:
                    await conn.execute(LOG_FINISH_SQL, job.log_id, "interrupted", result.reason)
                    await conn.execute(
                        UNLOCK_AND_DECREMENT_SQL, job.place_id, job.site_domain, job.page_uri
                    )
                    return

                outcome = "success" if result.success else "error"
                await conn.execute(LOG_FINISH_SQL, job.log_id, outcome, result.reason)

                if result.success:
                    await enqueue_children(conn, job, result.links)
                    await conn.execute(
                        INSERT_SUCCESS_SQL,
                        job.place_id, job.site_domain, job.page_uri,
                        result.final_uri, result.http_status, result.r2_key,
                        result.bytes, result.emails or None, job.attempts,
                    )
                    await conn.execute(
                        DELETE_QUEUE_RECORD_SQL, job.place_id, job.site_domain, job.page_uri
                    )
                elif job.attempts >= max_attempts:
                    await conn.execute(
                        INSERT_ERROR_SQL,
                        job.place_id, job.site_domain, job.page_uri,
                        result.http_status, job.attempts, result.reason,
                    )
                    await conn.execute(
                        DELETE_QUEUE_RECORD_SQL, job.place_id, job.site_domain, job.page_uri
                    )
                else:
                    await conn.execute(
                        UNLOCK_QUEUE_RECORD_SQL, job.place_id, job.site_domain, job.page_uri
                    )
    except CONNECTION_ERRORS as e:
        log.warning(f"DB unavailable recording {job.place_id} {job.page_uri!r}: {e!r}")
    except Exception:
        log.exception(f"record_outcome failed for {job.place_id} {job.page_uri!r}")


def log_outcome(worker_id: int, job: ClaimedJob, result: ScrapeResult, max_attempts: int) -> None:
    tag = job.page_uri or "<root>"
    if result.success:
        log.info(f"w{worker_id} success {job.place_id} {tag} "
                 f"emails={len(result.emails)} links={len(result.links)}")
    elif result.reason == SHUTDOWN_REASON:
        log.info(f"w{worker_id} interrupted {job.place_id} {tag} (unlocked, will retry)")
    elif job.attempts >= max_attempts:
        log.info(f"w{worker_id} terminal {job.place_id} {tag} reason={result.reason}")
    else:
        log.info(f"w{worker_id} retry {job.place_id} {tag} "
                 f"attempt={job.attempts} reason={result.reason}")


async def sleep_or_shutdown(shutdown_event: asyncio.Event, seconds: float) -> bool:
    """Sleep up to 'seconds' or return early if shutdown is signaled.
    Returns True when shutdown is signaled, False on timeout."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def claim_with_retry(
    pool: asyncpg.Pool, shutdown_event: asyncio.Event, poll_interval_s: float,
) -> ClaimedJob | None:
    """Claim one page. On DB error, empty queue, or unexpected exception, sleep
    poll_interval_s (or until shutdown) and return None; the caller loops back."""
    job: ClaimedJob | None = None
    try:
        job = await claim_and_log_start(pool)
    except CONNECTION_ERRORS as e:
        log.warning(f"DB unavailable claiming: {e!r}; retrying in {poll_interval_s}s")
    except Exception:
        log.exception("claim failed")
    if job is None:
        await sleep_or_shutdown(shutdown_event, poll_interval_s)
    return job


async def handle_one_job(
    worker_id: int,
    browser: Browser,
    bucket: aioboto3.Session,
    job: ClaimedJob,
    pool: asyncpg.Pool,
    shutdown_event: asyncio.Event,
    max_attempts: int,
) -> None:
    """Scrape one claimed page, record, log. Revert on shutdown"""
    log.info(f"w{worker_id} claim {job.place_id} {job.page_uri or '<root>'} attempt={job.attempts}")
    try:
        result = await process_one_job(browser, bucket, job)
    except Exception as e:
        result = ScrapeResult(success=False, reason=f"unhandled: {e!r}")

    # revert attempt
    if not result.success and shutdown_event.is_set():
        result = ScrapeResult(success=False, reason=SHUTDOWN_REASON)

    await record_outcome(pool, job, result, max_attempts)
    log_outcome(worker_id, job, result, max_attempts)


async def worker_loop(
    worker_id: int,
    browser: Browser,
    bucket: aioboto3.Session,
    pool: asyncpg.Pool,
    shutdown_event: asyncio.Event,
    poll_interval_s: float,
    max_attempts: int,
) -> None:
    while not shutdown_event.is_set():
        job = await claim_with_retry(pool, shutdown_event, poll_interval_s)
        if job is None:
            continue
        await handle_one_job(
            worker_id, browser, bucket, job, pool, shutdown_event, max_attempts
        )
    log.info(f"w{worker_id} exit")


async def open_pool_with_retry(
    worker_count: int, poll_interval_s: float, shutdown_event: asyncio.Event,
) -> asyncpg.Pool | None:
    """Create the queue pool, retrying every poll_interval_s if the DB is
    unreachable. Returns None if shutdown was signaled before a pool opened."""
    while not shutdown_event.is_set():
        try:
            return await asyncpg.create_pool(
                QUEUE_DB_URL, min_size=2, max_size=worker_count + 2
            )
        except CONNECTION_ERRORS as e:
            log.warning(f"DB unavailable: {e!r}; retrying in {poll_interval_s}s")
            if await sleep_or_shutdown(shutdown_event, poll_interval_s):
                return None
    return None


def shutdown_handler(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def on_shutdown() -> None:
        if not shutdown_event.is_set():
            log.info("Shutdown signal received, draining workers...")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_shutdown)


async def run_service(
    headless: bool = True,
    worker_count: int = WORKER_COUNT,
    poll_interval_s: float = POLL_INTERVAL_S,
    max_attempts: int = MAX_ATTEMPTS,
    respect_sitemap: bool = False,
    respect_robots: bool = False,
) -> None:
    """Run workers until SIGTERM/SIGINT. One shared browser + R2 client. Each
    worker scrapes pages in its own context"""
    if respect_sitemap or respect_robots:
        log.warning("respect_sitemap/respect_robots are not honored yet; crawling normally")

    shutdown_event = asyncio.Event()
    shutdown_handler(shutdown_event)

    log.info(f"Service started, workers={worker_count}")
    pool = await open_pool_with_retry(worker_count, poll_interval_s, shutdown_event)
    if pool is None:
        log.info("Shutdown clean")
        return

    session = aioboto3.Session()
    try:
        async with session.client(  # pyright: ignore
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_PAGES_ACCESS_KEY,
            aws_secret_access_key=R2_PAGES_SECRET_ACCESS_KEY,
            region_name="auto",
        ) as bucket:  # pyright: ignore
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                try:
                    await asyncio.gather(*(
                        worker_loop(
                            i, browser, bucket, pool, shutdown_event, # pyright: ignore[reportUnknownArgumentType]
                            poll_interval_s, max_attempts,
                        )
                        for i in range(worker_count)
                    ))
                finally:
                    try:
                        await browser.close()
                    except Exception as e:
                        log.info(f"browser close skipped: {e!r}")
    finally:
        await pool.close()
    log.info("Shutdown clean")


async def main(
    headless: bool = True,
    worker_count: int = WORKER_COUNT,
    max_attempts: int = MAX_ATTEMPTS,
    poll_interval_s: float = POLL_INTERVAL_S,
    respect_sitemap: bool = False,
    respect_robots: bool = False,
) -> None:
    await run_service(
        headless, worker_count, poll_interval_s, max_attempts,
        respect_sitemap, respect_robots,
    )


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
    parser.add_argument("--workers", default=WORKER_COUNT, type=bounded(int, 1, 512),
        help=f"INT     parallel workers, default {WORKER_COUNT}")
    parser.add_argument("--max-attempts", default=MAX_ATTEMPTS, type=bounded(int, 1, 32),
        help=f"INT     max attempts per page, default {MAX_ATTEMPTS}")
    parser.add_argument("--poll-interval", default=POLL_INTERVAL_S, type=bounded(float, 0.01, 3_600.0),
        help=f"FLOAT   poll interval per worker in seconds, default {POLL_INTERVAL_S}")
    parser.add_argument("--no-headless", action="store_true",
        help="run chromium with a visible window (calibration only)")
    parser.add_argument("--respect-sitemap", action="store_true",
        help="(not yet honored) crawl from sitemap when found on the root")
    parser.add_argument("--respect-robots-txt", action="store_true",
        help="(not yet honored) respect robots.txt crawl-delay")
    args = parser.parse_args()

    try:
        asyncio.run(main(
            headless=not args.no_headless,
            worker_count=args.workers,
            max_attempts=args.max_attempts,
            poll_interval_s=args.poll_interval,
            respect_sitemap=args.respect_sitemap,
            respect_robots=args.respect_robots_txt,
        ))
    except KeyboardInterrupt:
        log.info("Terminating")
    except CONNECTION_ERRORS:
        log.exception("DB unavailable")
    except Exception:
        log.exception("unhandled exception during shutdown")
