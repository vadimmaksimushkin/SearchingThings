BEGIN;

ALTER TABLE success
    ADD COLUMN IF NOT EXISTS resource_content_html JSONB,
    ADD COLUMN IF NOT EXISTS structured_content    JSONB;

COMMIT;