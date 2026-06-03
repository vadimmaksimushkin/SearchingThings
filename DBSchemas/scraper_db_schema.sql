-- FIXME: rename site_domain to page_root
CREATE TABLE IF NOT EXISTS scrape_queue (
    id              BIGSERIAL PRIMARY KEY,            -- FIFO
    place_id        TEXT NOT NULL,
    site_domain     TEXT NOT NULL,
    page_uri        TEXT NOT NULL,
    locked_until    TIMESTAMPTZ,                      -- NULL = available
    attempts        INTEGER NOT NULL DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ,
    UNIQUE (place_id, site_domain, page_uri)          -- identity / ON CONFLICT dedup target
);
CREATE INDEX IF NOT EXISTS idx_scrape_queue_available
    ON scrape_queue (id) WHERE locked_until IS NULL;

CREATE TABLE IF NOT EXISTS success (
    place_id    TEXT NOT NULL,
    site_domain TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    final_uri   TEXT NOT NULL,                        -- after redirects
    http_status INTEGER,
    r2_key      TEXT,                                 -- key in the R2 pages bucket
    bytes       INTEGER,
    emails      TEXT[],                               -- TEMP: scraper still regexes; drop when parser lands
    attempts    INTEGER NOT NULL DEFAULT 1,
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, site_domain, page_uri)
);

CREATE TABLE IF NOT EXISTS error (
    place_id    TEXT NOT NULL,
    site_domain TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    http_status INTEGER,                              -- NULL for DNS/connect failures
    attempts    INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'no error provided',
    failed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, site_domain, page_uri)
);

CREATE TABLE IF NOT EXISTS place_uri (
    place_id        TEXT NOT NULL,
    site_domain     TEXT NOT NULL,
    pages_remaining INTEGER NOT NULL DEFAULT 100,
    sitemap         TEXT[],
    robots_disallow TEXT[],                           -- cached Disallow rules
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, site_domain)
);

CREATE TABLE IF NOT EXISTS attempt_log (
    log_id      BIGSERIAL PRIMARY KEY,
    place_id    TEXT NOT NULL,
    site_domain TEXT NOT NULL,
    page_uri    TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,                          -- NULL = worker died before updating
    outcome     TEXT NOT NULL DEFAULT 'unknown',      -- 'success' | 'error' | 'unknown'
    reason      TEXT NOT NULL DEFAULT 'scraper did not update the log'
);
CREATE INDEX IF NOT EXISTS idx_attempt_log_place_id ON attempt_log (place_id);

CREATE TABLE IF NOT EXISTS extractor_state (
    id                    SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_scanned_at       TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    last_emails_synced_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'
);
INSERT INTO extractor_state (id) VALUES (1) ON CONFLICT DO NOTHING;
