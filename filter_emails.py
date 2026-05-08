"""Filter junk out of email fields in scraped malls JSON files.

Handles two shapes:
  - malls_scraped.json:    list of {place_id, emails: [...], ...}
  - malls_with_emails.json: list of {place_id, email: [...], ...}

Default output is a sibling file with `.cleaned.json` suffix. Use --in-place
to overwrite, or --dry-run to skip writing.
"""
import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net",
    "domain.com", "yourdomain.com",
    "dominio.com", "tudominio.com", "tu-dominio.com",
    "ejemplo.com", "ejemplo.com.mx",
    "test.com", "foo.com", "bar.com",
    "email.com", "correo.com", "sitio.com",
    "mail.com",   # in this dataset, only `ejemplo@mail.com` etc. — all placeholders
    "xxx.xxx",
}

SENTRY_SUBSTRINGS = ("sentry",)

# auto-generated hosting subdomains (left over from page asset URLs, never real mail hosts)
AUTOGEN_HOST_SUFFIXES = (
    ".azurewebsites.net",
)

# common typos for real TLDs
TYPO_TLDS = {"ccom", "cpm"}

ASSET_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "ico", "tif", "tiff",
    "css", "js", "mjs", "json", "xml",
    "woff", "woff2", "ttf", "eot", "otf",
    "mp4", "webm", "mp3", "pdf", "zip",
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})$")
# Leading JSON-unicode-escape leftovers in the local part (e.g. `u003e`=`>`, `u002F`=`/`).
# The scraper html-unescapes but doesn't json-unescape, so these prefix real emails.
ESCAPE_PREFIX_RE = re.compile(r"^(u00[0-9a-fA-F]{2})+")


def clean(email: str) -> str:
    """Return a normalized form of `email` with stripped prefix junk.

    Does not validate; returns empty string if cleaning makes it unusable.
    """
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    local = ESCAPE_PREFIX_RE.sub("", local)
    if not local:
        return ""
    return f"{local}@{domain}"


def classify(email: str) -> str | None:
    """Return a reason string if junk, else None."""
    m = EMAIL_RE.match(email)
    if not m:
        return "malformed"
    domain = m.group(1).lower()
    tld = domain.rsplit(".", 1)[-1]
    if tld in ASSET_EXTS:
        return f"asset(.{tld})"
    if tld in TYPO_TLDS:
        return f"typo_tld(.{tld})"
    if any(s in domain for s in SENTRY_SUBSTRINGS):
        return "sentry"
    if domain in PLACEHOLDER_DOMAINS:
        return "placeholder"
    if any(domain.endswith(s) for s in AUTOGEN_HOST_SUFFIXES):
        return "autogen_host"
    return None


def detect_field(data: list[dict]) -> str | None:
    for cand in ("emails", "email"):
        if any(cand in d for d in data):
            return cand
    return None


@dataclass
class Stats:
    total_in: int = 0
    total_out: int = 0
    affected: int = 0
    fully_emptied: int = 0
    cleaned: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list[str]] = field(default_factory=dict)
    removed_all: list[tuple[str, str]] = field(default_factory=list)

    def record_drop(self, email: str, reason: str, capture_full: bool) -> None:
        self.reasons[reason] += 1
        self.samples.setdefault(reason, [])
        if len(self.samples[reason]) < 5:
            self.samples[reason].append(email)
        if capture_full:
            self.removed_all.append((email, reason))


def process_emails(emails: list[str], stats: Stats, capture_full: bool) -> list[str]:
    keep: list[str] = []
    seen: set[str] = set()
    dropped_here = 0
    for original in emails:
        cleaned = clean(original)
        if cleaned != original:
            stats.cleaned += 1
        reason = classify(cleaned) if cleaned else "malformed"
        if reason is not None:
            dropped_here += 1
            stats.record_drop(original, reason, capture_full)
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        keep.append(cleaned)
    if dropped_here:
        stats.affected += 1
    if emails and not keep:
        stats.fully_emptied += 1
    return keep


def filter_data(data: list[dict], field_name: str, capture_full: bool) -> Stats:
    stats = Stats()
    for item in data:
        emails = item.get(field_name)
        if emails is None:
            continue
        stats.total_in += len(emails)
        item[field_name] = process_emails(emails, stats, capture_full)
        stats.total_out += len(item[field_name])
    return stats


def print_report(path: Path, field_name: str, n_records: int, stats: Stats,
                 show_removed: bool) -> None:
    print(f"File:                {path}")
    print(f"Field:               {field_name!r}")
    print(f"Records:             {n_records}")
    print(f"Emails before:       {stats.total_in}")
    print(f"Emails after:        {stats.total_out}")
    print(f"Removed:             {stats.total_in - stats.total_out}")
    print(f"Cleaned (in place):  {stats.cleaned}")
    print(f"Records affected:    {stats.affected}")
    print(f"Records emptied:     {stats.fully_emptied}")
    print()
    print("Removed by reason:")
    for r, c in stats.reasons.most_common():
        sample_str = ", ".join(stats.samples[r])
        print(f"  {c:>5}  {r:<18}  e.g. {sample_str}")
    if show_removed:
        print()
        print("All removed entries:")
        for e, r in stats.removed_all:
            print(f"  [{r}] {e}")


def resolve_output(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output
    if args.in_place:
        return args.path
    return args.path.with_suffix(".cleaned.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path)
    ap.add_argument("--in-place", action="store_true", help="overwrite the input file")
    ap.add_argument("--dry-run", action="store_true", help="don't write any output")
    ap.add_argument("--output", type=Path, default=None, help="explicit output path")
    ap.add_argument("--show-removed", action="store_true",
                    help="print every removed email")
    args = ap.parse_args()

    with open(args.path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        sys.exit(f"Expected a JSON list at {args.path}, got {type(data).__name__}")

    field_name = detect_field(data)
    if field_name is None:
        sys.exit(f"No 'emails' or 'email' field found in {args.path}")

    stats = filter_data(data, field_name, args.show_removed)
    print_report(args.path, field_name, len(data), stats, args.show_removed)

    if args.dry_run:
        print("\n(dry-run; nothing written)")
        return

    out = resolve_output(args)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
