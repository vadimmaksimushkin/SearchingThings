-- Requires PostGIS extension:
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS main_types (
    main_type    TEXT PRIMARY KEY,
    counter      INTEGER NOT NULL DEFAULT 10,  -- decrements on stage-3 live search; populate trigger at 0
    populated_at TIMESTAMPTZ,                  -- NULL = not populated
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS main_type_labels (
    main_type TEXT NOT NULL REFERENCES main_types(main_type) ON DELETE CASCADE,
    lang_code TEXT NOT NULL
        CHECK (length(lang_code) = 2 AND lang_code = lower(lang_code)),
    label     TEXT NOT NULL,
    PRIMARY KEY (main_type, lang_code)
);

CREATE TABLE IF NOT EXISTS places (
    place_id        TEXT PRIMARY KEY,
    main_type       TEXT NOT NULL REFERENCES main_types(main_type),
    name            TEXT,
    address         TEXT,
    phone           TEXT,
    website         TEXT,
    rating          REAL,
    rating_count    INTEGER,
    geog            geography(Point, 4326) NOT NULL,
    plus_code       TEXT,
    category        TEXT[],
    opening_hours           JSONB,
    secondary_opening_hours JSONB,
    emails          TEXT[],                  -- NULL = no emails recorded
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    -- preview_photo lives on the places_with_preview view (below), sourced
    -- from photos.bucket_key WHERE is_preview = TRUE.
);
CREATE INDEX IF NOT EXISTS places_type_geog_gix ON places USING GIST (main_type, geog);
CREATE INDEX IF NOT EXISTS places_geog_only_gix ON places USING GIST (geog); -- For KNN search

CREATE TABLE IF NOT EXISTS reviews (
    place_id          TEXT NOT NULL REFERENCES places(place_id) ON DELETE CASCADE,
    name              TEXT NOT NULL,         -- API "name": "places/.../reviews/..."
    rating            INTEGER,
    text              TEXT,                  -- from originalText.text
    language_code     TEXT,                  -- from originalText.languageCode
    author_name       TEXT,
    author_uri        TEXT,
    author_photo      TEXT,
    published_at      TIMESTAMPTZ,
    flag_content_uri  TEXT,
    google_maps_uri   TEXT,
    raw               JSONB NOT NULL,
    PRIMARY KEY (place_id, name)
);

CREATE TABLE IF NOT EXISTS photos (
    place_id             TEXT NOT NULL REFERENCES places(place_id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,      -- API "name", the photo reference
    width_px             INTEGER,
    height_px            INTEGER,
    author_attributions  JSONB,              -- plural; array of authors in API
    google_maps_uri      TEXT,
    flag_content_uri     TEXT,
    raw                  JSONB NOT NULL,
    bucket_key           TEXT,                                       -- NULL = not yet scraped to R2
    is_preview           BOOLEAN NOT NULL DEFAULT FALSE,             -- exactly one TRUE per place_id
    PRIMARY KEY (place_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS photos_one_preview_per_place
    ON photos (place_id) WHERE is_preview;

CREATE TABLE IF NOT EXISTS scraped_results (
    place_id              TEXT PRIMARY KEY REFERENCES places(place_id) ON DELETE CASCADE,
    scraped_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    emails                TEXT[],
    resource_content_html JSONB,         -- {page_main:…, page_about:…} full original page dump
    structured_content    JSONB          -- {description, services, catalog}
);

-- Exposes the light structured_content only; heavy resource_content_html is
-- intentionally left off the view. Never SELECT * this view in list queries —
-- name columns so the scraped_results join is eliminated when content isn't read.
CREATE OR REPLACE VIEW places_with_preview AS
SELECT
    p.*,
    ph.bucket_key AS preview_photo,
    sr.structured_content
FROM places p
LEFT JOIN photos ph
       ON ph.place_id = p.place_id
      AND ph.is_preview
LEFT JOIN scraped_results sr
       ON sr.place_id = p.place_id;
