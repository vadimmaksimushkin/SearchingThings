# FIXME: finish code cleanup
import asyncio
import html as html_lib
import random
import re
import sys
import time
import asyncpg
import aioboto3  # pyright: ignore[reportMissingTypeStubs]
import botocore.exceptions  # pyright: ignore[reportMissingTypeStubs]
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
from seleniumbase import cdp_driver  # pyright: ignore[reportMissingTypeStubs]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import (
    QUEUE_DB_URL,
    R2_ACCOUNT_ID,
    R2_PAGES_ACCESS_KEY,
    R2_PAGES_BUCKET,
    R2_PAGES_SECRET_ACCESS_KEY,
)
from constants import ASSET_EXTS, CONTACT_KEYWORDS

WORKER_COUNT = 3
MAX_ATTEMPTS = 3
POLL_INTERVAL_S = 1.0
PAGES_PER_BROWSER = 60
MIN_DOMAIN_DELAY_S = 5.0
MAX_DOMAIN_DELAY_S = 15.0
PAGE_TIMEOUT_MS = 15_000
LOCK_DURATION = timedelta(minutes=5.0)
BROWSER_LANG = "en-US"
BROWSER_TIMEZONE = "America/Mexico_City"
LAUNCH_ARGS = [
    "--use-gl=angle",
    "--use-angle=gl-egl",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--screen-info={1366x768}",
    "--window-size=1366,728",
]
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_URL_EXTS = {"pdf", "zip", "exe", "dmg", "tar", "gz"}
SHUTDOWN_REASON = "shutdown_interrupted"
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
CONNECTION_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.InternalClientError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)
BUCKET_ERRORS = (
    botocore.exceptions.BotoCoreError,
    botocore.exceptions.ClientError,
)

CLAIM_SQL = """
UPDATE scrape_queue
SET locked_until = now() + $1,
    attempts = attempts + 1,
    last_attempt_at = now()
WHERE id = (
    SELECT id FROM scrape_queue
    WHERE (locked_until IS NULL OR locked_until < now())
      AND page_root <> ALL($2::text[])
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, page_root, page_uri, attempts
"""

LOG_START_SQL = """
INSERT INTO attempt_log (page_root, page_uri, attempt_no)
VALUES ($1, $2, $3)
RETURNING log_id
"""

LOG_FINISH_SQL = """
UPDATE attempt_log
SET finished_at = now(), outcome = $2, reason = $3
WHERE log_id = $1
"""

INSERT_SUCCESS_SQL = """
INSERT INTO success
    (page_root, page_uri, final_uri, http_status, r2_key, bytes, emails, attempts)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (page_root, page_uri) DO NOTHING
"""

INSERT_ERROR_SQL = """
INSERT INTO error (page_root, page_uri, http_status, attempts, reason)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (page_root, page_uri) DO NOTHING
"""

DELETE_QUEUE_RECORD_SQL = """
DELETE FROM scrape_queue WHERE page_root = $1 AND page_uri = $2
"""

UNLOCK_QUEUE_RECORD_SQL = """
UPDATE scrape_queue SET locked_until = NULL
WHERE page_root = $1 AND page_uri = $2
"""

# Unlock AND roll back the attempt when a worker is interrupted by shutdown
UNLOCK_AND_DECREMENT_SQL = """
UPDATE scrape_queue
SET locked_until = NULL,
    attempts = GREATEST(attempts - 1, 0)
WHERE page_root = $1 AND page_uri = $2
"""

LOCK_PAGE_CAP_SQL = """
SELECT pages_remaining FROM page
WHERE page_root = $1
FOR UPDATE
"""

FETCH_KNOWN_PAGES_SQL = """
SELECT page_uri FROM scrape_queue WHERE page_root = $1
UNION ALL SELECT page_uri FROM success WHERE page_root = $1
UNION ALL SELECT page_uri FROM error   WHERE page_root = $1
"""

INSERT_CHILD_SQL = """
INSERT INTO scrape_queue (page_root, page_uri)
VALUES ($1, $2)
ON CONFLICT (page_root, page_uri) DO NOTHING
"""

DECREMENT_BUDGET_SQL = """
UPDATE page SET pages_remaining = GREATEST(pages_remaining - $2, 0)
WHERE page_root = $1
"""


class Counter:
    def __init__(self, limit: int) -> None:
        log.info(f"Counter is initialized with limit={limit}")
        self.limit = limit
        self._counter = 0
        self.reached = asyncio.Event()

    def increment(self) -> None:
        self._counter += 1
        if self._counter >= self.limit:
            self.reached.set()


class DomainThrottle:
    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        lease_s: float,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.lease_s = lease_s
        self.busy_until: dict[str, float] = {}

    def busy_domains(self) -> list[str]:
        now = time.monotonic()
        expired = [d for d, deadline in self.busy_until.items() if deadline <= now]
        for d in expired:
            del self.busy_until[d]
        return list(self.busy_until.keys())

    def reserve(self, domain: str) -> None:
        self.busy_until[domain] = time.monotonic() + self.lease_s

    def cooldown(self, domain: str) -> None:
        delay = random.uniform(
            self.min_delay,
            self.max_delay
        )
        self.busy_until[domain] = time.monotonic() + delay
        log.info(f"[DomainThrottle] Set delay on {domain} for {delay}s")


class MaxPagesReached(Exception):
    pass


class TerminateService(Exception):
    pass


@dataclass
class ClaimedJob:
    id: int
    log_id: int
    page_root: str
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



def shutdown_handler(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def on_shutdown() -> None:
        if not shutdown_event.is_set():
            log.info("Shutdown signal received, exiting...")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_shutdown)


async def sleep_or_shutdown(
    shutdown_event: asyncio.Event,
    seconds: float,
) -> bool:
    """Return `true` on firing shutdown event"""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


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


async def clean_ua(page: Page) -> None:
    """Strip the `HeadlessChrome` token from the live UA via CDP. Kept dynamic
    (not hardcoded) since SeleniumBase auto-updates its bundled Chrome."""
    real_ua = await page.evaluate("() => navigator.userAgent")
    clean = real_ua.replace("HeadlessChrome", "Chrome")
    cdp = await page.context.new_cdp_session(page)
    await cdp.send( # pyright: ignore[reportUnknownMemberType]
        "Network.setUserAgentOverride",
        {"userAgent": clean}
    )


async def scroll_to_bottom(
    page: Page,
    max_steps: int = 20,
    step_pause_ms: int = 300,
) -> None:
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


def is_priority_link(url: str) -> bool:
    """Return true for contact links"""
    path = urlparse(url).path.lower()
    return any(kw in path for kw in CONTACT_KEYWORDS)


def normalize_host_path(url: str) -> str:
    """Scheme and www insensitive 'host+path'"""
    p = urlparse(url)
    host = (p.hostname or "")
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    return f"{host}{path}".lower()


def is_same_site(page_root: str, url: str) -> bool:
    base = normalize_host_path(page_root)
    target = normalize_host_path(url)
    return target == base or target.startswith(base + "/")


def page_r2_key(final_uri: str) -> str:
    p = urlparse(final_uri)
    rest = f"{p.hostname or ''}{p.path}".rstrip("/")
    return f"{rest}.html"


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


async def discover_links(page: Page, page_root: str) -> set[str]:
    """Collect same-site page URLs. Skips assets and the root itself"""
    base = normalize_host_path(page_root)
    links: set[str] = set()
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
        links.add(normalize_url(absolute))
    return links


async def enqueue_children(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    job: ClaimedJob,
    links: set[str],
) -> int:
    """Filter discovered links against the known links, cap at the remainings,
    insert and decrement the budget by the number enqueued.
    Returns inserted count."""
    remaining: int | None = await conn.fetchval(
        LOCK_PAGE_CAP_SQL,
        job.page_root,
    )
    if not remaining or not links:
        return 0
    known_rows = await conn.fetch(
        FETCH_KNOWN_PAGES_SQL,
        job.page_root,
    )
    known = {r["page_uri"] for r in known_rows}
    candidates = (u for u in links if u not in known)
    to_insert = sorted(
        candidates, key=lambda u: (0 if is_priority_link(u) else 1, u)
    )[:remaining]
    if not to_insert:
        return 0
    await conn.executemany(
        INSERT_CHILD_SQL,
        [(job.page_root, u) for u in to_insert],
    )
    await conn.execute(
        DECREMENT_BUDGET_SQL,
        job.page_root,
        len(to_insert)
    )
    return len(to_insert)


async def record_outcome(
    pool: asyncpg.Pool,
    job: ClaimedJob,
    result: ScrapeResult,
    max_attempts: int,
) -> None:
    """Update DB on outcome, update attempt log, transfer record from queue
    to success or error, or unlock it for future use or unlock and decrement
    on shutdown interruption"""
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if result.reason == SHUTDOWN_REASON:
                    await conn.execute(
                        LOG_FINISH_SQL,
                        job.log_id,
                        "interrupted",
                        result.reason,
                    )
                    await conn.execute(
                        UNLOCK_AND_DECREMENT_SQL,
                        job.page_root,
                        job.page_uri,
                    )
                    return

                outcome = "success" if result.success else "error"
                await conn.execute(
                    LOG_FINISH_SQL,
                    job.log_id,
                    outcome,
                    result.reason
                )

                if result.success:
                    await enqueue_children(conn, job, result.links)
                    await conn.execute(
                        INSERT_SUCCESS_SQL,
                        job.page_root,
                        job.page_uri,
                        result.final_uri,
                        result.http_status,
                        result.r2_key,
                        result.bytes,
                        result.emails or None,
                        job.attempts,
                    )
                    await conn.execute(
                        DELETE_QUEUE_RECORD_SQL,
                        job.page_root,
                        job.page_uri,
                    )
                elif job.attempts >= max_attempts:
                    await conn.execute(
                        INSERT_ERROR_SQL,
                        job.page_root,
                        job.page_uri,
                        result.http_status,
                        job.attempts,
                        result.reason,
                    )
                    await conn.execute(
                        DELETE_QUEUE_RECORD_SQL,
                        job.page_root,
                        job.page_uri,
                    )
                else:
                    await conn.execute(
                        UNLOCK_QUEUE_RECORD_SQL,
                        job.page_root,
                        job.page_uri,
                    )
    except CONNECTION_ERRORS as e:
        log.warning(f"DB unavailable recording {job.page_root} {job.page_uri!r}:"
                    f" {e!r}")
    except Exception:
        log.exception(f"record_outcome failed for {job.page_root}"
                      f" {job.page_uri!r}")


def log_outcome(
    worker_number: int,
    job: ClaimedJob,
    result: ScrapeResult,
    max_attempts: int
) -> None:
    tag = job.page_uri or "<root>"
    if result.success:
        log.info(f"w{worker_number} success {job.page_root} {tag} "
                 f"emails={len(result.emails)} links={len(result.links)}")
    elif result.reason == SHUTDOWN_REASON:
        log.info(f"w{worker_number} interrupted {job.page_root} {tag}"
                 f" (unlocked, will retry)")
    elif job.attempts >= max_attempts:
        log.info(f"w{worker_number} terminal {job.page_root} {tag}"
                 f" reason={result.reason}")
    else:
        log.info(f"w{worker_number} retry {job.page_root} {tag} "
                 f"attempt={job.attempts} reason={result.reason}")



async def claim_and_log_start(
    pool: asyncpg.Pool,
    busy_domains: list[str],
) -> ClaimedJob | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(CLAIM_SQL, LOCK_DURATION, busy_domains)
            if not row:
                return None
            log_id = await conn.fetchval(
                LOG_START_SQL, row["page_root"],
                row["page_uri"], row["attempts"],
            )
            return ClaimedJob(
                id=row["id"],
                log_id=log_id,
                page_root=row["page_root"],
                page_uri=row["page_uri"],
                attempts=row["attempts"],
            )


async def scrape(
    context: BrowserContext,
    bucket: aioboto3.Session,
    job: ClaimedJob
) -> ScrapeResult:
    target = job.page_uri or job.page_root
    page = await context.new_page()
    try:
        await clean_ua(page)
        try:
            response: Response | None = await page.goto(
                target, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
            )
        except PWTimeout:
            return ScrapeResult(
                success=False,
                final_uri=target,
                reason="page_load_timeout"
            )
        except Exception as e:
            return ScrapeResult(
                success=False,
                final_uri=target,
                reason=f"goto_failed: {e!r}"
            )

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
            links = await discover_links(page, job.page_root)
        except Exception as e:
            log.warning(f"  link discovery failed on {final_uri}: {e!r}")
            links: set[str] = set()

        body = html.encode("utf-8", "replace")
        key = page_r2_key(final_uri)
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
        try:
            await page.close()
        except Exception as e:
            log.info(f"page close skipped: {e!r}")


async def job_handler(
    worker_number: int,
    pool: asyncpg.Pool,
    bucket: aioboto3.Session,
    shutdown_event: asyncio.Event,
    max_attempts: int,
    context: BrowserContext,
    job: ClaimedJob,
) -> None:
    """Scrape one claimed page, record, log. Revert on shutdown"""
    log.info(f"w{worker_number} claim {job.page_root} {job.page_uri or '<root>'}"
             f" attempt={job.attempts}")
    try:
        result = await scrape(context, bucket, job)
    except Exception as e:
        result = ScrapeResult(success=False, reason=f"unhandled: {e!r}")

    # revert attempt
    if not result.success and shutdown_event.is_set():
        result = ScrapeResult(success=False, reason=SHUTDOWN_REASON)

    await record_outcome(pool, job, result, max_attempts)
    log_outcome(worker_number, job, result, max_attempts)


async def worker_loop(
    worker_number: int,
    pool: asyncpg.Pool,
    bucket: aioboto3.Session,
    shutdown_event: asyncio.Event,
    max_attempts: int,
    poll_interval_s: float,
    counter: Counter,
    throttle: DomainThrottle,
    claim_lock: asyncio.Lock,
    context: BrowserContext,
):
    while not shutdown_event.is_set() and not counter.reached.is_set():
        log.info(f"w{worker_number}: Service is runnig")
        counter.increment()

        job: ClaimedJob | None = None
        try:
            async with claim_lock:
                busy = throttle.busy_domains()
                job = await claim_and_log_start(pool, busy)
                if job is not None:
                    throttle.reserve(job.page_root)
        except CONNECTION_ERRORS as e:
            log.warning(f"DB unavailable claiming: {e!r}; retrying in {poll_interval_s}s")
        except Exception:
            log.exception("claim failed")
        if job is None:
            await sleep_or_shutdown(shutdown_event, poll_interval_s)
            continue

        try:
            await job_handler(
                worker_number,
                pool,
                bucket,
                shutdown_event,
                max_attempts,
                context,
                job,
            )
        finally:
            throttle.cooldown(job.page_root)
        await sleep_or_shutdown(shutdown_event, poll_interval_s)
        if shutdown_event.is_set():
            log.info(f"w{worker_number}: Successful exit")


async def service_loop(
    headless: bool,
    worker_count: int,
    max_attempts: int,
    poll_interval_s: float,
    pages_per_browser: int,
    min_domain_delay: float,
    max_domain_delay: float,
    shutdown_event: asyncio.Event,
    pool: asyncpg.Pool,
    bucket: aioboto3.Session,
) -> None:
    """
    Set shutdown event handling, restart browser every N pages and start
    M worker loops
    """
    throttle = DomainThrottle(
        min_domain_delay, max_domain_delay, LOCK_DURATION.total_seconds()
    )
    claim_lock = asyncio.Lock()
    while not shutdown_event.is_set():
        try:
            p = await async_playwright().start()
            driver = await cdp_driver.start_async( # pyright:ignore
                headless=headless,
                browser_executable_path=p.chromium.executable_path,
                lang=BROWSER_LANG,
                tzone=BROWSER_TIMEZONE,
                browser_args=LAUNCH_ARGS,
            )
            browser: Browser = await p.chromium.connect_over_cdp(
                driver.get_endpoint_url()
            )
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context()
            await context.route("**/*", block_heavy_assets)
            counter = Counter(pages_per_browser)
            await asyncio.gather(*(
                worker_loop(
                    i,
                    pool,
                    bucket,
                    shutdown_event,
                    max_attempts,
                    poll_interval_s,
                    counter,
                    throttle,
                    claim_lock,
                    context,
                ) for i in range(worker_count)
            ))

            if counter.reached.is_set(): raise MaxPagesReached

        except MaxPagesReached:
            log.info(f"{pages_per_browser} pages handled,"
                " restarting the browser...")
        finally:
            try:
                await driver.quit() # type: ignore
            except Exception:
                pass
            if context: await context.close() # pyright: ignore
            if browser: await browser.close() # pyright: ignore
            if p: await p.stop() # type: ignore
    return None


async def main(
    headless: bool,
    worker_count: int,
    max_attempts: int,
    poll_interval_s: float,
    pages_per_browser: int,
    min_domain_delay: float,
    max_domain_delay: float,
) -> None:
    """
    Open DB connections and start service_loop
    """
    shutdown_event = asyncio.Event()
    shutdown_handler(shutdown_event)
    # opening DB connections and error handling to start `service_loop`
    while not shutdown_event.is_set():
        try:
            async with asyncpg.create_pool(
                QUEUE_DB_URL,
                min_size=2,
                max_size=worker_count + 2
            ) as pool:
                session = aioboto3.Session()
                async with session.client(  # pyright: ignore
                    "s3",
                    endpoint_url=R2_ENDPOINT,
                    aws_access_key_id=R2_PAGES_ACCESS_KEY,
                    aws_secret_access_key=R2_PAGES_SECRET_ACCESS_KEY,
                    region_name="auto",
                ) as bucket: # pyright: ignore[reportUnknownVariableType]
                    await service_loop(
                        headless,
                        worker_count,
                        max_attempts,
                        poll_interval_s,
                        pages_per_browser,
                        min_domain_delay,
                        max_domain_delay,
                        shutdown_event,
                        pool,
                        bucket, # type: ignore
                    )
        except CONNECTION_ERRORS as e:
            log.warning(f"DB unavailable: {e!r};"
                        f" retrying in {poll_interval_s}s")
            if await sleep_or_shutdown(shutdown_event, poll_interval_s):
                return None
        except BUCKET_ERRORS as e:
            log.warning(f"Bucket unavailable: {e!r};"
                        f" retrying in {poll_interval_s}s")
            if await sleep_or_shutdown(shutdown_event, poll_interval_s):
                return None
        except Exception as e:
            return None


    return None


log = logging.getLogger(__name__)
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    def bounded[T: (int, float)](
        t: type[T],
        low: T,
        high: T
    ) -> Callable[[str], T]:
        """argparse type: parse with type(), then enforce low <= value <= high"""
        def check(s: str) -> T:
            try:
                v = t(s)
            except ValueError:
                message = f"expected {t.__name__}, got {s!r}"
                raise argparse.ArgumentTypeError(message)
            if not (low <= v <= high):
                message = f"must be in [{low}, {high}], got {v}"
                raise argparse.ArgumentTypeError(message)
            return v
        return check

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run in headed mode"
    )
    parser.add_argument(
        "--workers",
        default=WORKER_COUNT,
        type=bounded(int, 1, 512),
        help=f"Number of parallel workers, INT default {WORKER_COUNT}"
    )
    parser.add_argument(
        "--max-attempts",
        default=MAX_ATTEMPTS,
        type=bounded(int, 1, 32),
        help=f"Max attempts per page, INT default {MAX_ATTEMPTS}"
    )
    parser.add_argument(
        "--poll-interval",
        default=POLL_INTERVAL_S,
        type=bounded(float, 0.01, 3_600.0),
        help=f"Poll interval per worker in seconds,"
        f" FLOAT default {POLL_INTERVAL_S}"
    )
    parser.add_argument(
        "--pages-per-browser",
        default=PAGES_PER_BROWSER,
        type=bounded(int, 1, 200),
        help=f"Number of ticks to handle before restaring the brower,"
        f" INT default {PAGES_PER_BROWSER}"
    )
    parser.add_argument(
        "--min-domain-delay",
        default=MIN_DOMAIN_DELAY_S,
        type=bounded(float, 0.0, 3_600.0),
        help=f"Min delay between handling pages from one site domain,"
        f" FLOAT default {MIN_DOMAIN_DELAY_S}"
    )
    parser.add_argument(
        "--max-domain-delay",
        default=MAX_DOMAIN_DELAY_S,
        type=bounded(float, 0.0, 3_600.0),
        help=f"Max delay between handling pages from one site domain,"
        f" FLOAT default {MAX_DOMAIN_DELAY_S}"
    )
    args = parser.parse_args()
    if args.min_domain_delay > args.max_domain_delay:
        parser.error("--min-domain-delay must be <= --max-domain-delay")
    headless = not args.headed
    worker_count = args.workers
    max_attempts = args.max_attempts
    poll_interval_s = args.poll_interval
    pages_per_browser = args.pages_per_browser
    min_domain_delay = args.min_domain_delay
    max_domain_delay = args.max_domain_delay

    try:
        asyncio.run(main(
            headless,
            worker_count,
            max_attempts,
            poll_interval_s,
            pages_per_browser,
            min_domain_delay,
            max_domain_delay,
        ))
    except KeyboardInterrupt:
        log.info("Terminating")
    except CONNECTION_ERRORS:
        log.exception("DB unavailable")
    except Exception:
        log.exception("unhandled exception during shutdown")
