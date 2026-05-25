-- Schema for image_scraper almost identical to email scraper
BEGIN;

CREATE TABLE IF NOT EXISTS scrape_queue (
    id              SERIAL PRIMARY KEY,
    place_id        TEXT NOT NULL,
    photo_name      TEXT NOT NULL,           -- full ref, from photos.name
    google_maps_uri TEXT NOT NULL,
    locked_until    TIMESTAMPTZ,             -- NULL = available
    attempts        INTEGER NOT NULL DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ,
    UNIQUE (place_id, photo_name)
);

CREATE TABLE IF NOT EXISTS success (
    place_id   TEXT NOT NULL,
    photo_name TEXT NOT NULL,
    bucket_key TEXT NOT NULL,
    webp_bytes INTEGER NOT NULL,             -- size of the WebP uploaded to R2
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (place_id, photo_name)
);

CREATE TABLE IF NOT EXISTS error (
    place_id   TEXT NOT NULL,
    photo_name TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT 'no error provided',
    failed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, photo_name)
);

CREATE TABLE IF NOT EXISTS attempt_log (
    log_id      BIGSERIAL PRIMARY KEY,
    place_id    TEXT NOT NULL,
    photo_name  TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    outcome     TEXT NOT NULL DEFAULT 'unknown',   -- 'success' | 'error' | 'unknown'
    reason      TEXT NOT NULL DEFAULT 'scraper did not update the log'
);
CREATE INDEX idx_attempt_log_place_id ON attempt_log(place_id);

CREATE TABLE IF NOT EXISTS extractor_state (
    id                    SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_scanned_at       TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    last_images_synced_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'
);
INSERT INTO extractor_state (id) VALUES (1) ON CONFLICT DO NOTHING;

COMMIT;