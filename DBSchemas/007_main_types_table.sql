-- main_types + main_type_labels as whitelisted labels
BEGIN;

CREATE TABLE main_types (
    main_type    TEXT PRIMARY KEY,
    counter      INTEGER NOT NULL DEFAULT 10,
    populated_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- gym and shopping_mall are already populated in places; bowling and
-- billiard start unpopulated with the default counter=10 trigger.
INSERT INTO main_types (main_type, counter, populated_at) VALUES
    ('gym',           0, NOW()),
    ('shopping_mall', 0, NOW()),
    ('bowling', DEFAULT, DEFAULT),
    ('billiard', DEFAULT, DEFAULT);

CREATE TABLE main_type_labels (
    main_type TEXT NOT NULL REFERENCES main_types(main_type) ON DELETE CASCADE,
    lang_code TEXT NOT NULL
        CHECK (length(lang_code) = 2 AND lang_code = lower(lang_code)),
    label     TEXT NOT NULL,
    PRIMARY KEY (main_type, lang_code)
);

INSERT INTO main_type_labels (main_type, lang_code, label) VALUES
    ('gym',           'en', 'Gym'),
    ('gym',           'es', 'Gimnasio'),
    ('shopping_mall', 'en', 'Shopping Mall'),
    ('shopping_mall', 'es', 'Centro Comercial'),
    ('bowling', 'en', 'Bowling'),
    ('bowling', 'es', 'Boliche'),
    ('billiard', 'en', 'Billiard'),
    ('billiard', 'es', 'Billar');

-- Verification
DO $$
DECLARE missing TEXT;
BEGIN
    SELECT string_agg(DISTINCT p.main_type, ', ')
      INTO missing
      FROM places p
      LEFT JOIN main_types m ON m.main_type = p.main_type
     WHERE m.main_type IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION
            'places has main_type values not in main_types: %', missing;
    END IF;
END $$;

ALTER TABLE places
    ADD CONSTRAINT places_main_type_fkey
    FOREIGN KEY (main_type) REFERENCES main_types(main_type);
    -- ON UPDATE / ON DELETE defaults to NO ACTION:
COMMIT;
