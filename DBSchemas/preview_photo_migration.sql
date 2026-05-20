-- Add preview_photo to places.
--
-- Run order: standalone, additive only. Safe to apply with API running.

BEGIN;

ALTER TABLE places
    ADD COLUMN preview_photo TEXT;

COMMIT;
