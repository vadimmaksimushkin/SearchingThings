-- Add preview_photo to places.
BEGIN;

ALTER TABLE places
    ADD COLUMN preview_photo TEXT;

COMMIT;
