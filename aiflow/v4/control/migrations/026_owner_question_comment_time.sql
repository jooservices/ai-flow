-- A decision row is created before the triage worker posts its Jira question. Store the
-- timestamp returned by Jira per ticket so an earlier owner comment cannot answer it.
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS question_comment_at timestamptz;
