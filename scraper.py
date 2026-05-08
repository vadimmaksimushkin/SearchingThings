import asyncio
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import html as html_lib

from playwright.async_api import Browser, Page, TimeoutError as PWTimeout, async_playwright

from playwright_stealth import Stealth # pyright: ignore[reportMissingTypeStubs]
# from ShoppingMall import ShoppingMall, ShoppingMallList

CONCURRENCY = 10
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
EMAIL_RE_PATTERN = r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
PAGE_TIMEOUT_MS = 10_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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
    "ayuda","conocenos",
    # English
    "contact", "contact-us", "contactus",
    "about", "about-us", "aboutus",
    "info",
]
CONTACT_RE = re.compile(r"(?i)(?:^|[/\-_?#=])(" + "|".join(re.escape(k) for k in CONTACT_KEYWORDS) + r")(?:$|[/\-_?#&.])")


@dataclass
class Mall:
    place_id: str
    website_url: str
    emails: list[str] = field(default_factory=list[str])
    error: str | None = None

async def scroll_to_bottom(page: Page, max_steps: int = 20, step_pause_ms: int = 100) -> None:
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
        # height: int = await page.evaluate("() => document.body.scrollHeight")
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
        href = urljoin(page.url, href)  # Make absolute if not
        if CONTACT_RE.search(href):
            contact_urls.add(normalize_url(href))
    return contact_urls

async def get_emails(page: Page) -> set[str]:
    html_decoded = html_lib.unescape(await page.content())
    emails = set(EMAIL_RE.findall(html_decoded))
    return emails

async def scrape_mall(mall: Mall, browser: Browser):
    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/Mexico_City",
        viewport={"width": 1366, "height": 768},
    )
    try:
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        await _goto(page, mall.website_url)
        emails = await get_emails(page)
        print(len(emails), "main page", emails)

        contact_urls: set[str] = await get_contact_urls(page)
        print(len(contact_urls), "found contact URLs", contact_urls)

        for contact_url in contact_urls:
            if await _goto(page, contact_url):
                emails_from_contact_page = await get_emails(page)
                emails.update(emails_from_contact_page)

        mall.emails = list(emails)
        print(len(mall.emails), "all pages", mall.emails)
    finally:
        await context.close()


async def scrape():
    malls = [
        Mall("ChIJs4nT1xoC0oURRpC7xl0_Pbg", "http://www.antara.com.mx/"),
        Mall("", ""),
        Mall("", ""),
        ]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def run(mall: Mall):
        async with sem:
            try:
                await scrape_mall(mall, browser)
            except Exception as e:
                mall.error = str(e)
                print(f"  scrape_mall failed for {mall.website_url}: {e}", file=sys.stderr)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await asyncio.gather(*(run(m) for m in malls))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(scrape())
