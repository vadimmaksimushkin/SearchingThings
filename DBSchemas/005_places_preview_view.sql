-- Rename places.preview_photo -> preview_photo_backup and create the
-- Places now queried as places_with_preview
BEGIN;

ALTER TABLE places RENAME COLUMN preview_photo TO preview_photo_backup;

CREATE VIEW places_with_preview AS
SELECT
    p.*,
    ph.bucket_key AS preview_photo
FROM places p
LEFT JOIN photos ph
       ON ph.place_id = p.place_id
      AND ph.is_preview;

COMMIT;
