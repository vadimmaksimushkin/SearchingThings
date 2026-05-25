-- Rename link_extractor_state -> extractor_state on m2's pg_queue.
BEGIN;

ALTER TABLE link_extractor_state RENAME TO extractor_state;

COMMIT;