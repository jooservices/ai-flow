-- First-run setup progress and the public address selected in the browser.
CREATE TABLE IF NOT EXISTS setup_wizard_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    public_url text NOT NULL DEFAULT '',
    skipped_steps text[] NOT NULL DEFAULT ARRAY[]::text[],
    sample_ticket_key text NOT NULL DEFAULT '',
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO setup_wizard_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING;
