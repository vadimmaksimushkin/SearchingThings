CREATE TABLE scrape_queue (
    id              SERIAL PRIMARY KEY,
    place_id        TEXT NOT NULL UNIQUE,
    website         TEXT NOT NULL,
    locked_until    TIMESTAMPTZ,                 -- NULL = available
    attempts        INTEGER NOT NULL DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ                  -- temporary, drop after debugging
);

CREATE TABLE success (
    place_id      TEXT NOT NULL,
    website       TEXT NOT NULL,
    scraped_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    emails        TEXT[],                          -- NULL = scraped, none found
    final_website TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (place_id, website)
);

CREATE TABLE error (
    place_id   TEXT NOT NULL,
    website    TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT 'no error provided',
    failed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, website)
);

CREATE TABLE attempt_log (
    log_id      BIGSERIAL PRIMARY KEY,
    place_id    TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,                -- 1, 2, 3 for this place
    website     TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,                     -- NULL = worker died before updating
    outcome     TEXT NOT NULL DEFAULT 'unknown', -- 'success' | 'error' | 'unknown'
    reason      TEXT NOT NULL DEFAULT 'scraper did not update the log'
);
CREATE INDEX idx_attempt_log_place_id ON attempt_log(place_id);

CREATE TABLE link_extractor_state (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_scanned_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'
);
INSERT INTO link_extractor_state (id) VALUES (1) ON CONFLICT DO NOTHING;
