-- Requires PostGIS extension:
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE places (
    place_id        TEXT PRIMARY KEY,
    main_type       TEXT NOT NULL,           -- e.g. 'shopping_mall', 'gym'
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
);
CREATE INDEX places_type_geog_gix ON places USING GIST (main_type, geog);
CREATE INDEX places_geog_only_gix ON places USING GIST (geog); -- For KNN search

CREATE TABLE reviews (
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

CREATE TABLE photos (
    place_id             TEXT NOT NULL REFERENCES places(place_id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,      -- API "name", the photo reference
    width_px             INTEGER,
    height_px            INTEGER,
    author_attributions  JSONB,              -- plural; array of authors in API
    google_maps_uri      TEXT,
    flag_content_uri     TEXT,
    raw                  JSONB NOT NULL,
    PRIMARY KEY (place_id, name)
);
