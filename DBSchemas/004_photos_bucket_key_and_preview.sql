-- Add bucket_key + is_preview to photos on m1's pg_places.
BEGIN;

ALTER TABLE photos ADD COLUMN bucket_key TEXT;
ALTER TABLE photos ADD COLUMN is_preview BOOLEAN NOT NULL DEFAULT FALSE;

-- Fill is_preview for existing rows: one row per place_id, MIN(ctid)
-- Approximation of first inserted photo as preview
WITH first_per_place AS (
    SELECT DISTINCT ON (place_id) place_id, ctid
    FROM photos
    ORDER BY place_id, ctid
)
UPDATE photos p SET is_preview = TRUE
FROM first_per_place f
WHERE p.ctid = f.ctid;

CREATE UNIQUE INDEX IF NOT EXISTS photos_one_preview_per_place
    ON photos (place_id) WHERE is_preview;

COMMIT;