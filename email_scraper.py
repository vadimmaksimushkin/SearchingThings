# FIXME: check what's going on here
"""
Read links.json, visit each mall website with Playwright stealth, and try
to extract a contact email. Crawls the homepage plus a few same-domain
"contact"-ish links (one hop). Writes results back to a copy of
malls_example.json as malls_with_emails.json.

Match strategy: place_id when present, else exact website URL.
"""
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright
from playwright_stealth import Stealth # pyright: ignore[reportMissingTypeStubs]

from places import ShoppingMallList


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Substrings that mark a hit as junk (placeholders, asset filenames, etc.)
EMAIL_BLOCKLIST = (
    "example.com", "example.org", "domain.com", "yoursite.com", "yourcompany.com",
    "email.com", "test.com", "sentry.io", "wixpress.com",
)
ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico")

CONTACT_KEYWORDS = (
    "contact", "contacto", "contactanos", "contáctanos", "contact-us", "contactus",
    "kontakt",
)

PAGE_TIMEOUT_MS = 20_000
MAX_CONTACT_PAGES = 3


@dataclass
class ScrapeResult:
    place_id: str | None
    name: str | None
    website: str
    emails: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def best_email(self) -> str | None:
        return self.emails[0] if self.emails else None


def _registrable_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _is_valid_email(addr: str) -> bool:
    low = addr.lower()
    if any(b in low for b in EMAIL_BLOCKLIST):
        return False
    if any(low.endswith(ext) for ext in ASSET_EXTS):
        return False
    if low.split("@", 1)[0] in {"", "u003e", "u003c"}:
        return False
    return True


def _rank_emails(emails: Iterable[str], site_host: str) -> list[str]:
    """Same-domain emails first, then everything else; deduped, order-preserved."""
    seen: set[str] = set()
    same_domain: list[str] = []
    other: list[str] = []
    for raw in emails:
        addr = raw.strip().lower()
        if addr in seen or not _is_valid_email(addr):
            continue
        seen.add(addr)
        host = addr.split("@", 1)[1]
        if site_host and (host == site_host or host.endswith("." + site_host)):
            same_domain.append(addr)
        else:
            other.append(addr)
    return same_domain + other


def _harvest_emails(page: Page) -> list[str]:
    found: list[str] = []
    try:
        mailtos = page.eval_on_selector_all(
            "a[href^='mailto:']",
            "els => els.map(e => e.getAttribute('href'))",
        )
    except Exception:
        mailtos = []
    for href in mailtos or []:
        if not href:
            continue
        addr = href.split(":", 1)[1].split("?", 1)[0].strip()
        if addr:
            found.append(addr)
    try:
        html = page.content()
    except Exception:
        html = ""
    found.extend(EMAIL_RE.findall(html))
    return found


def _find_contact_urls(page: Page, base_url: str) -> list[str]:
    site_host = _registrable_host(base_url)
    try:
        anchors = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.getAttribute('href') || '', text: (e.textContent || '').trim()}))",
        )
    except Exception:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for a in anchors or []:
        href = (a.get("href") or "").strip()
        text = (a.get("text") or "").strip().lower()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if _registrable_host(absolute) != site_host:
            continue
        haystack = (absolute + " " + text).lower()
        if not any(k in haystack for k in CONTACT_KEYWORDS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        candidates.append(absolute)
        if len(candidates) >= MAX_CONTACT_PAGES:
            break
    return candidates


def _goto(page: Page, url: str) -> bool:
    try:
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        return True
    except PWTimeout:
        return False
    except Exception as e:
        print(f"  goto error on {url}: {e}", file=sys.stderr)
        return False


def scrape_one(page: Page, link: dict) -> ScrapeResult:
    url = link["website"]
    result = ScrapeResult(
        place_id=link.get("place_id"),
        name=link.get("name"),
        website=url,
    )
    if not _goto(page, url):
        result.error = "homepage unreachable"
        return result

    emails = _harvest_emails(page)
    contact_urls = _find_contact_urls(page, page.url)
    for c in contact_urls:
        if _goto(page, c):
            emails.extend(_harvest_emails(page))

    result.emails = _rank_emails(emails, _registrable_host(url))
    return result


def scrape_all(
    links_path: str | Path = "links.json",
    headless: bool = True,
) -> list[ScrapeResult]:
    with open(links_path, encoding="utf-8") as f:
        links = json.load(f)

    results: list[ScrapeResult] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        for i, link in enumerate(links, 1):
            print(f"[{i}/{len(links)}] {link.get('name')} -> {link['website']}")
            try:
                r = scrape_one(page, link)
            except Exception as e:
                r = ScrapeResult(
                    place_id=link.get("place_id"),
                    name=link.get("name"),
                    website=link["website"],
                    error=str(e),
                )
            if r.best_email:
                print(f"   {r.best_email}")
            elif r.error:
                print(f"   ! {r.error}")
            else:
                print("   (no email found)")
            results.append(r)
        browser.close()
    return results


def write_malls_with_emails(
    results: list[ScrapeResult],
    source: str | Path = "malls_example.json",
    out_path: str | Path = "malls_with_emails.json",
) -> int:
    by_place_id = {r.place_id: r for r in results if r.place_id and r.best_email}
    by_website = {r.website: r for r in results if r.best_email}

    malls = ShoppingMallList.from_json_file(source)
    updated = 0
    for mall in malls:
        match = None
        if mall.place_id and mall.place_id in by_place_id:
            match = by_place_id[mall.place_id]
        elif mall.website and mall.website in by_website:
            match = by_website[mall.website]
        if match and match.best_email:
            mall.email = match.best_email
            updated += 1
    malls.to_json_file(out_path)
    return updated


if __name__ == "__main__":
    results = scrape_all()
    n_updated = write_malls_with_emails(results)
    found = sum(1 for r in results if r.best_email)
    print(f"\nemails found: {found}/{len(results)} | malls updated: {n_updated}")
    print(f"wrote malls_with_emails.json")
