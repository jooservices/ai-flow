-- Match repeated owner questions by their normalized text, independent of triage-generated keywords.
ALTER TABLE owner_decisions ADD COLUMN IF NOT EXISTS question_key text NOT NULL DEFAULT '';

-- Existing pending asks stored the question before the first newline and the triage rationale afterwards.
UPDATE owner_decisions
SET question_key = trim(regexp_replace(lower(split_part(question, E'\n', 1)), '[^[:alnum:]]+', ' ', 'g'))
WHERE status='asked' AND question_key='';

CREATE INDEX IF NOT EXISTS owner_decisions_question_key
    ON owner_decisions (question_key) WHERE status='asked' AND question_key<>'';
