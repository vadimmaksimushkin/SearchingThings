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
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urljoin, urlparse, urlunparse
from playwright.async_api import Browser, Page, TimeoutError as PWTimeout, async_playwright
from playwright_stealth import Stealth  # pyright: ignore[reportMissingTypeStubs]

from api_key import QUEUE_DB_URL
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

CONTACT_RE = re.compile(
    r"(?i)(?:^|[/\-_?#=])("
    + "|".join(re.escape(k) for k in CONTACT_KEYWORDS)
    + r")(?:$|[/\-_?#&.])"
)

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
    """Scan page for URLs and collect ones with contact keywords in them"""
    urls = await page.locator("a[href]").all()
    contact_urls: set[str] = set()
    for url in urls:
        href = await url.get_attribute("href")
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        href = urljoin(page.url, href)
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


# FIXME: Maybe preserve context on a browser and not to create new to each website
async def scrape_one_site(browser: Browser, website: str) -> ScrapeResult:
    """Crawl one website and its contact/about URLs"""
    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/Mexico_City",
        viewport={"width": 1366, "height": 768},
    )
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
        contact_urls = await get_contact_urls(page)

        # FIXME: ???wrap to try/except to preserve scraped results from main page??
        for contact_url in contact_urls:
            if await _goto(page, contact_url):
                emails.update(await get_emails(page))

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


# BUG: REWORK as main source of bugs
async def worker_loop(
    worker_id: int,
    browser: Browser,
    pool: asyncpg.Pool,
    poll_interval_s: float = POLL_INTERVAL_S) -> None:
    """MAIN SOURCE OF BUGS"""
    while True:
        try:
            job = await claim_and_log_start(pool)
        except Exception as e:
            log.warning(f"w{worker_id} claim failed: {e!r}")
            await asyncio.sleep(poll_interval_s)
            continue
        if job is None:
            await asyncio.sleep(poll_interval_s)
            continue

        log.info(f"w{worker_id} claim {job.place_id} attempt={job.attempts}")
        try:
            result = await scrape_one_site(browser, job.website)
        except Exception as e:
            result = ScrapeResult(success=False, final_website=job.website, reason=repr(e))

        try:
            await record_outcome(pool, job, result)
        except Exception as e:
            log.warning(f"w{worker_id} record_outcome failed for {job.place_id}: {e!r}")
            continue

        if result.success:
            log.info(f"w{worker_id} success {job.place_id} emails={len(result.emails)}")
        elif job.attempts >= MAX_ATTEMPTS:
            log.info(f"w{worker_id} terminal {job.place_id} reason={result.reason}")
        else:
            log.info(f"w{worker_id} retry {job.place_id} attempt={job.attempts} reason={result.reason}")


async def main(
    worker_count: int = WORKER_COUNT,
    max_attempts: int = MAX_ATTEMPTS,
    poll_interval_s: float = POLL_INTERVAL_S) -> None:
    """Open connection pool and initialize browser"""
    async with asyncpg.create_pool(QUEUE_DB_URL, min_size=2, max_size=worker_count + 2) as pool:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                log.info(f"scraper started: workers={worker_count} " +
                         f"max_attempts={max_attempts}")
                await asyncio.gather(
                    *(worker_loop(i, browser, pool, poll_interval_s) for i in range(worker_count))
                )
            finally:
                await browser.close()


# FIXME: Input sanitization
# FIXME: Database is down exception
# FIXME: Exit signals handling
# FIXME: Handle 403, denied, facebook login page and other page blockers
# FIXME: Potentially handle cookie banner
# FIXME: Cap the page size download and memory usage or worker
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--workers", default=WORKER_COUNT, type=int,
        help=f"INT     Specity the amount of parallel workers, default {WORKER_COUNT}")
    argument_parser.add_argument("--max-attempts", default=MAX_ATTEMPTS, type=int,
        help=f"INT     Specity the amount of parallel workers, default {WORKER_COUNT}")
    argument_parser.add_argument("--poll-interval", default=POLL_INTERVAL_S, type=float,
        help=f"FLOAT    Poll interval of each worker in seconds, default {POLL_INTERVAL_S}")
    args = argument_parser.parse_args()
    worker_count = args.workers
    max_attempts = args.max_attempts
    poll_interval_s = args.poll_interval

    asyncio.run(main(worker_count, max_attempts, poll_interval_s))
