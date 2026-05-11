import asyncio, json, re, sys

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import html as html_lib
from playwright.async_api import Browser, Page, TimeoutError as PWTimeout, async_playwright
from playwright_stealth import Stealth # pyright: ignore[reportMissingTypeStubs]

from ShoppingMall import ShoppingMallList
from constants import ASSET_EXTS

CONCURRENCY = 10
SAVE_EVERY = 25
SOURCE_MALLS_PATH = "malls_5193.json"
SCRAPED_MALLS_PATH = "malls_scraped.json"
MERGED_MALLS_PATH = "malls_with_emails.json"
LINK_PATH = "links.json"
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
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


def load_malls_from_links(path: str | Path = LINK_PATH) -> list[Mall]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [
        Mall(place_id=item["place_id"], website_url=item.get("website") or "")
        for item in data
    ]


def load_malls_from_shoppingmalls(path: str | Path = SOURCE_MALLS_PATH) -> list[Mall]:
    sm_list = ShoppingMallList.from_json_file(path)
    out: list[Mall] = []
    for sm in sm_list:
        if not sm.place_id or not sm.website:
            continue
        out.append(Mall(place_id=sm.place_id, website_url=sm.website))
    return out


def load_scraped(path: str | Path = SCRAPED_MALLS_PATH) -> dict[str, Mall]:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return {item["place_id"]: Mall(**item) for item in data if item.get("place_id")}


def save_malls(malls: list[Mall], path: str | Path = SCRAPED_MALLS_PATH) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([mall.__dict__ for mall in malls], f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def update_emails_in_malls(
    scraped: list[Mall],
    malls_path: str | Path = SOURCE_MALLS_PATH,
    output_path: str | Path | None = None,
) -> None:
    by_id = {m.place_id: m for m in scraped if m.place_id and m.emails}
    malls = ShoppingMallList.from_json_file(malls_path)
    for sm in malls:
        scraped_mall = by_id.get(sm.place_id or "")
        if scraped_mall is None:
            continue
        sm.email = sorted(set(scraped_mall.emails))
    malls.to_json_file(output_path if output_path is not None else malls_path)

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

def is_asset_or_sentry(email: str) -> bool:
    domain = email.rpartition("@")[2].lower()
    top_level_domain = domain.rsplit(".", 1)[-1]
    if (top_level_domain in ASSET_EXTS) or ("sentry" in domain):
        return True
    # if "sentry" in domain:
    #     return True
    return False

async def get_emails(page: Page) -> set[str]:
    html_decoded = html_lib.unescape(await page.content())
    emails = {email for email in EMAIL_RE.findall(html_decoded) if not is_asset_or_sentry(email)}
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

        contact_urls: set[str] = await get_contact_urls(page)

        for contact_url in contact_urls:
            if await _goto(page, contact_url):
                emails_from_contact_page = await get_emails(page)
                emails.update(emails_from_contact_page)

        mall.emails = list(emails)
    finally:
        await context.close()


async def scrape_all(all_malls: list[Mall], done_by_id: dict[str, Mall], todo: list[Mall], done: list[Mall]):
    print(
        f"total={len(all_malls)} resumed={len(done)} to_scrape={len(todo)}",
        file=sys.stderr,
    )

    sem = asyncio.Semaphore(CONCURRENCY)
    save_lock = asyncio.Lock()

    async def run(mall: Mall):
        async with sem:
            try:
                await scrape_mall(mall, browser)
            except Exception as e:
                mall.error = str(e)
                print(f"  scrape_mall failed for {mall.website_url}: {e}", file=sys.stderr)
            async with save_lock:
                done.append(mall)
                if len(done) % SAVE_EVERY == 0:
                    save_malls(done, SCRAPED_MALLS_PATH)
                    print(f"  progress: {len(done)}/{len(all_malls)}", file=sys.stderr)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await asyncio.gather(*(run(m) for m in todo))
        finally:
            await browser.close()

# FIXME: add argument parsing to specify the files
# FIXME 2: Potential rework to service based with a DB for storing the URLs to scrape
# and a DB to push/update emails
if __name__ == "__main__":
    SOURCE_MALLS_PATH = "malls_5193.json"
    SCRAPED_MALLS_PATH = "malls_scraped.json"
    MERGED_MALLS_PATH = "malls_with_emails.json"
    LINK_PATH = "links.json"

    all_malls = load_malls_from_shoppingmalls(SOURCE_MALLS_PATH)
    # all_malls = load_malls_from_links()
    done_by_id = load_scraped(SCRAPED_MALLS_PATH)
    todo = [mall for mall in all_malls if mall.place_id not in done_by_id]
    done: list[Mall] = list(done_by_id.values())

    asyncio.run(scrape_all(all_malls, done_by_id, todo, done))

    save_malls(done, SCRAPED_MALLS_PATH)
    update_emails_in_malls(done, SOURCE_MALLS_PATH, MERGED_MALLS_PATH)
