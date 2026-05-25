-- Dangerous
BEGIN;

ALTER TABLE places DROP COLUMN preview_photo_backup;

COMMIT;
