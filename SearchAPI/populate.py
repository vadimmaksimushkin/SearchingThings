"""
Populating script that searches through optimized list of CDMX colonias
and upsert places into a local DB. Have lock to prevent race conditions
"""
import aiohttp
import asyncio
import asyncpg
import hashlib
import logging
import sys
from pathlib import Path
from typing import Any
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from constants import CDMX_COLONIAS_OPTIMIZED
from SearchAPI.google_fetch import LIVE_TEXT_SEARCH_MASK, paginated_search
from SearchAPI.local_db_query import (
    mark_populated,
    reset_counter,
    upsert_place_bundle,
)

log = logging.getLogger(__name__)

POPULATE_TIMEOUT_SECONDS = 10*60  # 30 min — ~3× expected populate duration
POPULATE_QUERY_TEMPLATE = "{label} en {colonia}, CDMX"
MAX_COLONIA_FAILURE_RATIO = 0.5

def hash_alg_int64(s: str) -> int:
    """Algorithm for advisory lock, uses blake2b → 8 bytes → signed int64"""
    h = hashlib.blake2b(s.encode(), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


async def do_populate(
    pool: asyncpg.Pool, main_type: str, label: str,
) -> None:
    """
    For each CDMX colonia, run Google textSearch '{label} en {colonia},
    CDMX' (3 pages, LIVE fields, no location restriction). Deduplication by
    place_id, then upsert each result. Advisory lock auto-releases
    on connection drop
    """
    async with aiohttp.ClientSession() as session:
        per_colonia = await asyncio.gather(
            *(
                paginated_search(
                    session,
                    POPULATE_QUERY_TEMPLATE.format(label=label, colonia=c),
                    LIVE_TEXT_SEARCH_MASK,
                    raise_on_error=True,
                )
                for c in CDMX_COLONIAS_OPTIMIZED
            ),
            return_exceptions=True,
        )

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    failures = 0
    for result in per_colonia:
        if isinstance(result, BaseException):
            failures += 1
            log.warning(f"[populate] colonia search failed: {result!r}")
            continue
        for raw in result:
            pid = raw.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(raw)

    total = len(CDMX_COLONIAS_OPTIMIZED)
    if total and failures / total > MAX_COLONIA_FAILURE_RATIO:
        raise RuntimeError(
            f"[populate] {main_type!r}: {failures}/{total} colonia searches "
            f"failed (> {MAX_COLONIA_FAILURE_RATIO:.0%}); aborting"
        )
    log.info(
        f"[populate] {main_type!r}: {len(unique)} unique places across "
        f"{total} colonias ({failures} colonia searches failed)"
    )

    await asyncio.gather(
        *(upsert_place_bundle(pool, main_type, raw, "populate") for raw in unique),
        return_exceptions=True,
    )
    log.info(f"[populate] {main_type!r}: upsert complete")


async def run_populate(pool: asyncpg.Pool, main_type: str, label: str) -> None:
    """
    Hold a Postgres advisory lock for the full populate,
    mark populated_at on success, reset counter to 1 on failure.
    Lock auto-releases on connection drop
    `label` is the localized search term used by Google textSearch
    """
    lock_id = hash_alg_int64(f"populate:{main_type}")
    async with pool.acquire() as conn:
        lock = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id)
        if not lock:
            log.info(f"[populate] {main_type!r} already in flight; skipping")
            return
        try:
            # Double check
            already = await conn.fetchval(
                "SELECT populated_at FROM main_types WHERE main_type = $1",
                main_type,
            )
            if already is not None:
                log.info(f"[populate] {main_type!r} already populated; skipping")
                return
            log.info(f"[populate] {main_type!r} starting")
            await asyncio.wait_for(
                do_populate(pool, main_type, label),
                timeout=POPULATE_TIMEOUT_SECONDS,
            )
            await mark_populated(conn, main_type)
            log.info(f"[populate] {main_type!r} done")
        except Exception:
            log.exception(f"[populate] {main_type!r} failed")
            await reset_counter(conn, main_type, value=1)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)
