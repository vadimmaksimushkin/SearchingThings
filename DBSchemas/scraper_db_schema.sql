CREATE TABLE IF NOT EXISTS scrape_queue (
    id              BIGSERIAL PRIMARY KEY,            -- FIFO
    page_root       TEXT NOT NULL,                    -- normalized root: scheme://host[:port]/path
    page_uri        TEXT NOT NULL,                    -- '' = the root itself; children live under page_root
    locked_until    TIMESTAMPTZ,                      -- NULL = available
    attempts        INTEGER NOT NULL DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ,
    UNIQUE (page_root, page_uri)                      -- identity / ON CONFLICT deduplicate target
);
CREATE INDEX IF NOT EXISTS idx_scrape_queue_available
    ON scrape_queue (id) WHERE locked_until IS NULL;

CREATE TABLE IF NOT EXISTS success (
    page_root   TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    final_uri   TEXT NOT NULL,                        -- after redirects
    http_status INTEGER,
    r2_key      TEXT,                                 -- key in the R2 pages bucket
    bytes       INTEGER,
    emails      TEXT[],                               -- TEMP: scraper still regexes; drop when parser lands
    attempts    INTEGER NOT NULL DEFAULT 1,
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (page_root, page_uri)
);

CREATE TABLE IF NOT EXISTS error (
    page_root   TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    http_status INTEGER,                              -- NULL for DNS/connect failures
    attempts    INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'no error provided',
    failed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (page_root, page_uri)
);

-- One row per crawled website root: page budget + cached crawl hints.
CREATE TABLE IF NOT EXISTS page (
    page_root       TEXT PRIMARY KEY,
    pages_remaining INTEGER NOT NULL DEFAULT 100,
    sitemap         TEXT[],
    robots_disallow TEXT[],                           -- cached Disallow rules
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Many places -> one page_root. Lets the extractor fan a root's scraped
-- emails back out to every place that points at it.
CREATE TABLE IF NOT EXISTS page_places (
    place_id  TEXT NOT NULL,
    page_root TEXT NOT NULL,
    PRIMARY KEY (place_id, page_root)
);
CREATE INDEX IF NOT EXISTS idx_page_places_root ON page_places (page_root);

CREATE TABLE IF NOT EXISTS attempt_log (
    log_id      BIGSERIAL PRIMARY KEY,
    page_root   TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,                          -- NULL = worker died before updating
    outcome     TEXT NOT NULL DEFAULT 'unknown',      -- 'success' | 'error' | 'unknown'
    reason      TEXT NOT NULL DEFAULT 'scraper did not update the log'
);
CREATE INDEX IF NOT EXISTS idx_attempt_log_page_root ON attempt_log (page_root);

CREATE TABLE IF NOT EXISTS extractor_state (
    id                    SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_scanned_at       TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    last_emails_synced_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'
);
INSERT INTO extractor_state (id) VALUES (1) ON CONFLICT DO NOTHING;
