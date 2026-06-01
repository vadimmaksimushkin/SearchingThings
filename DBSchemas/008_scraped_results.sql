BEGIN;

CREATE TABLE IF NOT EXISTS scraped_results (
    place_id              TEXT PRIMARY KEY REFERENCES places(place_id) ON DELETE CASCADE,
    scraped_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    emails                TEXT[],
    resource_content_html JSONB,         -- {page_main:…, page_about:…} full original resources dump
    structured_content    JSONB          -- {description, services, catalog}
);

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

COMMIT;