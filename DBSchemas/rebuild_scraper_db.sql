-- DROP scarper tables
BEGIN;

DROP TABLE IF EXISTS
    scrape_queue,
    success,
    error,
    attempt_log,
    place_uri,        -- renamed to `page`
    page,
    page_places,
    extractor_state
CASCADE;

COMMIT;
