-- Escalation classes and the owner decision register (docs/v4/escalations.md).
-- owner_decisions: what the owner already decided (status decided), what is being asked once (status asked) and
-- the amendments triage made to unreachable acceptance criteria (kind amendment).
CREATE TABLE IF NOT EXISTS owner_decisions (
    id bigserial PRIMARY KEY,
    topic text NOT NULL UNIQUE,
    kind text NOT NULL DEFAULT 'decision' CHECK (kind IN ('decision', 'amendment')),
    status text NOT NULL DEFAULT 'decided' CHECK (status IN ('decided', 'asked', 'retired')),
    project text NOT NULL DEFAULT '',
    title text NOT NULL,
    question text NOT NULL DEFAULT '',
    decision text NOT NULL DEFAULT '',
    options jsonb NOT NULL DEFAULT '[]'::jsonb,
    recommended text NOT NULL DEFAULT '',
    patterns jsonb NOT NULL DEFAULT '[]'::jsonb,
    source text NOT NULL DEFAULT 'owner',
    ticket_key text NOT NULL DEFAULT '',
    waiting_tickets text[] NOT NULL DEFAULT '{}'::text[],
    decided_by text NOT NULL DEFAULT '',
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    cited_count integer NOT NULL DEFAULT 0,
    last_cited_at timestamptz
);
CREATE INDEX IF NOT EXISTS owner_decisions_status ON owner_decisions (status, kind);

-- One row per escalation, classified when it is raised. reached_person is true only for the two classes that
-- need a person (new_business_decision, irreversible_or_production) once the owner is alerted.
CREATE TABLE IF NOT EXISTS escalations (
    id bigserial PRIMARY KEY,
    ticket_key text NOT NULL,
    project text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT 'dev',
    run_id text NOT NULL DEFAULT '',
    class text NOT NULL,
    question text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    decision_id bigint,
    resolution text NOT NULL DEFAULT 'open',
    reached_person boolean NOT NULL DEFAULT false,
    answer text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);
CREATE INDEX IF NOT EXISTS escalations_time ON escalations (created_at DESC);
CREATE INDEX IF NOT EXISTS escalations_ticket ON escalations (ticket_key, created_at DESC);

INSERT INTO owner_decisions (topic, title, question, decision, patterns, source, decided_by, decided_at) VALUES
('no-docker-for-ai-runs', 'No Docker for AI runs',
 'May the AI use Docker (or wait for a machine with Docker) to run its checks?',
 'No. Docker is not used for AI runs. Use the tools and test services (PostgreSQL, Redis) listed for the machine and do not ask for Docker.',
 $p$["\\bdocker\\b", "\\bdocker-compose\\b", "\\bpodman\\b"]$p$::jsonb, 'seed', 'owner', now()),
('no-release-or-tag-without-owner', 'No release or tag without the owner',
 'May the AI create a release, a version tag or publish a version?',
 'No. A release or tag is made only by the owner (or the automatic version release flow). Finish the change and its pull request without tagging or publishing.',
 $p$["\\b(?:create|cut|make|push|publish|prepare|do)\\b.{0,40}\\b(?:release|tag)\\b", "\\bgit tag\\b", "\\btag(?:ging)? (?:the )?(?:release|version|commit)\\b"]$p$::jsonb,
 'seed', 'owner', now()),
('company-name-jooservices', 'Name the company JOOservices',
 'What is the company name to use in text, documents and screens?',
 'JOOservices (one word: J, O, O, then lowercase services).',
 $p$["\\b(?:company|organi[sz]ation|brand|legal)\\b.{0,30}\\bname\\b", "\\bname\\b.{0,30}\\b(?:company|organi[sz]ation|brand)\\b"]$p$::jsonb,
 'seed', 'owner', now()),
('plans-and-limits', 'Plans and limits',
 'Which plans exist and what are their limits?',
 'Free: 1 machine, 2 AI accounts, 3 projects, 3 users. Team: 5 machines, 12 AI accounts, 25 projects, 25 users. Business: 50 machines, 100 AI accounts, 250 projects, 250 users. The owner edits them in Command Center; do not invent other plans or limits.',
 $p$["\\b(?:free|team|business) plan\\b", "\\bplan(?:s)?\\b.{0,40}\\blimits?\\b", "\\blimits?\\b.{0,40}\\bplan(?:s)?\\b", "\\bpricing\\b"]$p$::jsonb,
 'seed', 'owner', now()),
('quality-thresholds', 'Quality thresholds',
 'What coverage or quality threshold must a change meet?',
 'The project quality gates are fixed: unit test coverage of at least 90 percent and every check of the project must pass. A gate is not lowered or skipped to finish a ticket.',
 $p$["\\bcoverage\\b.{0,40}\\b(?:threshold|gate|target|minimum|percent|%)", "\\bquality (?:gate|threshold)s?\\b", "\\bminimum coverage\\b", "\\bsonar\\w*\\b.{0,40}\\bthreshold\\b"]$p$::jsonb,
 'seed', 'owner', now())
ON CONFLICT (topic) DO NOTHING;
