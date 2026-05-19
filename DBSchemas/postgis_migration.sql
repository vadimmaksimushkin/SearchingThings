-- PostGIS migration for the places table.
BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE places
    ADD COLUMN geog geography(Point, 4326);

UPDATE places
SET geog = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
WHERE latitude IS NOT NULL AND longitude IS NOT NULL;

ALTER TABLE places ALTER COLUMN geog SET NOT NULL;

CREATE INDEX places_type_geog_gix ON places USING GIST (main_type, geog);

ANALYZE places;

COMMIT;

-- --- Dangerous
-- BEGIN;

-- ALTER TABLE places
--     DROP COLUMN latitude,
--     DROP COLUMN longitude;

-- ANALYZE places;

-- COMMIT;
