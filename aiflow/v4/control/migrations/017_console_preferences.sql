-- Personal console preferences follow the signed-in person across browsers.
CREATE TABLE IF NOT EXISTS console_preferences (
    email text PRIMARY KEY,
    theme text NOT NULL CHECK (theme IN ('dark', 'light')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
