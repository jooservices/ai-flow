-- Several tickets may share a hot file when the owner allows it (docs/v4/merge-conflicts.md, hot_file_slots): the
-- reservation is one row per file and ticket instead of one holder per file.
ALTER TABLE hot_file_reservations DROP CONSTRAINT IF EXISTS hot_file_reservations_pkey;
ALTER TABLE hot_file_reservations ADD PRIMARY KEY (path, ticket_key);
