-- Rework pg_queue, drop and recreate everything except extractor_state
BEGIN;

DROP TABLE IF EXISTS scrape_queue;
DROP TABLE IF EXISTS success;
DROP TABLE IF EXISTS error;
DROP TABLE IF EXISTS attempt_log;

CREATE TABLE scrape_queue (
    id              BIGSERIAL PRIMARY KEY,            -- FIFO claim ordering
    place_id        TEXT NOT NULL,
    site_domain     TEXT NOT NULL,                    -- eTLD+1
    page_uri        TEXT NOT NULL,
    locked_until    TIMESTAMPTZ,                      -- NULL = available
    attempts        INTEGER NOT NULL DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ,
    UNIQUE (place_id, site_domain, page_uri)          -- identity / ON CONFLICT dedup target
);
-- claim scan: lowest id among available (unlocked) rows
CREATE INDEX idx_scrape_queue_available
    ON scrape_queue (id) WHERE locked_until IS NULL;

CREATE TABLE success (
    place_id    TEXT NOT NULL,
    site_domain TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    final_uri   TEXT NOT NULL,                        -- after redirects
    http_status INTEGER,
    r2_key      TEXT,                                 -- key in the R2 `Pages` bucket
    bytes       INTEGER,
    emails      TEXT[],                               -- TEMP: scraper still regexes; drop when parser lands
    attempts    INTEGER NOT NULL DEFAULT 1,
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, site_domain, page_uri)
);

CREATE TABLE error (
    place_id    TEXT NOT NULL,
    site_domain TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    http_status INTEGER,                              -- NULL for DNS/connect failures
    attempts    INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'no error provided',
    failed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, site_domain, page_uri)
);

CREATE TABLE place_uri (
    place_id        TEXT NOT NULL,
    site_domain     TEXT NOT NULL,
    pages_remaining INTEGER NOT NULL DEFAULT 100,     -- atomic countdown budget (cap = N)
    sitemap         TEXT[],                           -- NULL=not fetched, {}=none found, {...}=URLs
    robots_disallow TEXT[],                           -- cached Disallow rules
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, site_domain)
);

-- attempt_log realigned to the per-page identity (the old `website` column held
-- what is now page_uri). Audit-only; history is not preserved across the rework.
CREATE TABLE attempt_log (
    log_id      BIGSERIAL PRIMARY KEY,
    place_id    TEXT NOT NULL,
    site_domain TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,                     -- 1, 2, 3 for this page
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,                          -- NULL = worker died before updating
    outcome     TEXT NOT NULL DEFAULT 'unknown',      -- 'success' | 'error' | 'unknown'
    reason      TEXT NOT NULL DEFAULT 'scraper did not update the log'
);
CREATE INDEX idx_attempt_log_place_id ON attempt_log (place_id);

COMMIT;
