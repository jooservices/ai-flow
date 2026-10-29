-- Mark pre-marker ticket hand-offs so only existing rows may recover the old uncorrelated Jira comment.
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS legacy_question_comment boolean NOT NULL DEFAULT false;

UPDATE escalations e
SET legacy_question_comment = true
FROM owner_decisions d
WHERE d.id = e.decision_id
  AND d.kind = 'decision'
  AND d.status IN ('asked', 'decided')
  AND e.question_comment_at IS NULL;
