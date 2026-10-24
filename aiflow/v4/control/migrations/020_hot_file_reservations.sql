-- One holder per hot file while a dev run is claimed (docs/v4/merge-conflicts.md). A reservation is taken together
-- with the claim, under a lock, so two simultaneous claims cannot both start work on the same hot file.
CREATE TABLE IF NOT EXISTS hot_file_reservations (
    path        text PRIMARY KEY,
    ticket_key  text NOT NULL,
    run_id      text NOT NULL,
    reserved_at timestamptz NOT NULL DEFAULT now()
);
