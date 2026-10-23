-- Per-person console layouts (docs/v4/console-layouts.md): the order, size and visibility of the blocks of one console
-- page for one person. Stored on the server so the layout follows the person to another browser or computer.
CREATE TABLE IF NOT EXISTS console_layouts (
    email text NOT NULL,
    page text NOT NULL,
    blocks jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, page)
);
