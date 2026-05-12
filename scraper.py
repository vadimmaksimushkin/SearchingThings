"""Long-running email scraper service.

Drains scrape_queue: each worker atomically claims a row, scrapes the site
for emails, then writes the outcome to the success or error table and
finalizes the attempt_log audit row. Connects only to the queue DB.
"""
import asyncio
import html as html_lib
import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urljoin, urlparse, urlunparse

import asyncpg

from playwright.async_api import Browser, Page, TimeoutError as PWTimeout, async_playwright
from playwright_stealth import Stealth  # pyright: ignore[reportMissingTypeStubs]

from api_key import QUEUE_DB_URL
from constants import ASSET_EXTS

WORKER_COUNT = 10
MAX_ATTEMPTS = 3
LOCK_DURATION = timedelta(minutes=5)
POLL_INTERVAL_S = 1.0
PAGE_TIMEOUT_MS = 10_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

CONTACT_KEYWORDS = [
    # Spanish - contact
    "contacto", "contactos", "contactanos", "contactenos",
    "contáctanos", "contáctenos",
    # Spanish - about
    "nosotros", "quienes-somos", "quienessomos", "quienes_somos",
    "quiénes-somos", "acerca", "acerca-de", "acercade",
    "sobre-nosotros", "sobrenosotros",
    "empresa", "informacion", "información",
    "directorio", "directorio-de-contacto",
    "atencion", "atención", "atencion-a-clientes",
    "ayuda", "conocenos",
    # English
    "contact", "contact-us", "contactus",
    "about", "about-us", "aboutus",
    "info",
]
CONTACT_RE = re.compile(
    r"(?i)(?:^|[/\-_?#=])("
    + "|".join(re.escape(k) for k in CONTACT_KEYWORDS)
    + r")(?:$|[/\-_?#&.])"
)


@dataclass
class ClaimedJob:
    id: int
    place_id: str
    website: str
    attempts: int


@dataclass
class ScrapeResult:
    success: bool
    emails: list[str] = field(default_factory=list)
    final_website: str = ""
    reason: str = "ok"


# ---- page helpers ----

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


async def _goto(page: Page, url: str) -> bool:
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except PWTimeout:
        return False
    except Exception as e:
        print(f"  goto error on {url}: {e}", file=sys.stderr)
        return False
    try:
        await scroll_to_bottom(page)
    except Exception as e:
        print(f"  scroll error on {url}: {e}", file=sys.stderr)
    return True


def normalize_url(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


async def get_contact_urls(page: Page) -> set[str]:
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
    domain = email.rpartition("@")[2].lower()
    top_level_domain = domain.rsplit(".", 1)[-1]
    return (top_level_domain in ASSET_EXTS) or ("sentry" in domain)


async def get_emails(page: Page) -> set[str]:
    html_decoded = html_lib.unescape(await page.content())
    return {e for e in EMAIL_RE.findall(html_decoded) if not is_asset_or_sentry(e)}


# ---- the per-site scrape ----

async def scrape_one_site(browser: Browser, website: str) -> ScrapeResult:
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
                reason="main_page_load_failed",
            )

        final_website = page.url
        emails = await get_emails(page)
        contact_urls = await get_contact_urls(page)

        for contact_url in contact_urls:
            if await _goto(page, contact_url):
                emails.update(await get_emails(page))

        return ScrapeResult(
            success=True,
            emails=sorted(emails),
            final_website=final_website,
            reason="ok",
        )
    finally:
        await context.close()


# ---- DB operations ----

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
ON CONFLICT (place_id, attempt_no) DO UPDATE SET
    started_at  = now(),
    finished_at = NULL,
    outcome     = 'unknown',
    reason      = 'scraper did not update the log',
    website     = EXCLUDED.website
"""

LOG_FINISH_SQL = """
UPDATE attempt_log
SET finished_at = now(), outcome = $3, reason = $4
WHERE place_id = $1 AND attempt_no = $2
"""

INSERT_SUCCESS_SQL = """
INSERT INTO success (place_id, emails, final_website, attempts)
VALUES ($1, $2, $3, $4)
"""

INSERT_ERROR_SQL = """
INSERT INTO error (place_id, website, attempts, reason)
VALUES ($1, $2, $3, $4)
"""


async def claim_and_log_start(pool: asyncpg.Pool) -> ClaimedJob | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(CLAIM_SQL, LOCK_DURATION)
            if row is None:
                return None
            job = ClaimedJob(
                id=row["id"],
                place_id=row["place_id"],
                website=row["website"],
                attempts=row["attempts"],
            )
            await conn.execute(LOG_START_SQL, job.place_id, job.attempts, job.website)
            return job


async def record_outcome(
    pool: asyncpg.Pool, job: ClaimedJob, result: ScrapeResult
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            outcome = "success" if result.success else "error"
            await conn.execute(
                LOG_FINISH_SQL, job.place_id, job.attempts, outcome, result.reason
            )

            if result.success:
                await conn.execute(
                    "DELETE FROM scrape_queue WHERE place_id = $1", job.place_id
                )
                await conn.execute(
                    INSERT_SUCCESS_SQL,
                    job.place_id,
                    result.emails or None,
                    result.final_website,
                    job.attempts,
                )
            elif job.attempts >= MAX_ATTEMPTS:
                await conn.execute(
                    "DELETE FROM scrape_queue WHERE place_id = $1", job.place_id
                )
                await conn.execute(
                    INSERT_ERROR_SQL,
                    job.place_id,
                    job.website,
                    job.attempts,
                    result.reason,
                )
            else:
                await conn.execute(
                    "UPDATE scrape_queue SET locked_until = NULL WHERE place_id = $1",
                    job.place_id,
                )


# ---- worker loop ----

async def worker_loop(worker_id: int, browser: Browser, pool: asyncpg.Pool) -> None:
    while True:
        try:
            job = await claim_and_log_start(pool)
        except Exception as e:
            print(f"w{worker_id} claim failed: {e!r}", file=sys.stderr)
            await asyncio.sleep(POLL_INTERVAL_S)
            continue
        if job is None:
            await asyncio.sleep(POLL_INTERVAL_S)
            continue

        print(
            f"w{worker_id} claim {job.place_id} attempt={job.attempts}",
            file=sys.stderr,
        )
        try:
            result = await scrape_one_site(browser, job.website)
        except Exception as e:
            result = ScrapeResult(
                success=False, final_website=job.website, reason=repr(e)
            )

        try:
            await record_outcome(pool, job, result)
        except Exception as e:
            print(
                f"w{worker_id} record_outcome failed for {job.place_id}: {e!r}",
                file=sys.stderr,
            )
            continue

        if result.success:
            print(
                f"w{worker_id} success {job.place_id} emails={len(result.emails)}",
                file=sys.stderr,
            )
        elif job.attempts >= MAX_ATTEMPTS:
            print(
                f"w{worker_id} terminal {job.place_id} reason={result.reason}",
                file=sys.stderr,
            )
        else:
            print(
                f"w{worker_id} retry {job.place_id} attempt={job.attempts} "
                f"reason={result.reason}",
                file=sys.stderr,
            )


async def main() -> None:
    pool = await asyncpg.create_pool(
        QUEUE_DB_URL, min_size=2, max_size=WORKER_COUNT + 2
    )
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                print(
                    f"scraper started: workers={WORKER_COUNT} "
                    f"max_attempts={MAX_ATTEMPTS}",
                    file=sys.stderr,
                )
                await asyncio.gather(
                    *(worker_loop(i, browser, pool) for i in range(WORKER_COUNT))
                )
            finally:
                await browser.close()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
