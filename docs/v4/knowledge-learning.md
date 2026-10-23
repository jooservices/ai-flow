# Persistent learning and run context

AI Flow stores durable learning in `knowledge_items`. Completed runs create a decision record; tickets mirrored as
Done create a ticket study case after their active run finishes. Failed, timed-out, rework, blocked and escalated
runs create a study case with the recorded situation, cause, fix and prevention. Incidents create a pending case
when opened and update its resolution when closed. These writes are idempotent by their run, ticket or incident
source. A capture or embedding failure is logged and does not undo work or stop a claim.

The record includes its project or general scope, optional epic, kind, ticket key, run ID, Jira ticket URL, pull
request URL, source reference, capture time, approval state and handoff count. Development runs include the changed
file paths reported by the worker. The run's own rationale and explicit decisions are kept when present; otherwise
the record says they were not explicitly recorded and retains the ticket's stated reason and outcome.

## Scope and retrieval

Kinds are `study_case`, `decision`, `convention`, `skill` and `guide`. Project and epic records can be used only by
runs in that project; an epic record is available only to that epic and its descendants. General items have no
project or epic key and are offered to every project. An administrator can curate a project item into a general
lesson in the Knowledge screen after removing project-specific details.

When work is claimed through `/v1/work/claim` or the version-release claim endpoint, the control API builds a
context pack from approved, non-retired records in PostgreSQL. It includes project conventions and working rules,
recent decisions, stored history for the ticket and epic, and study cases ranked by PostgreSQL full-text search and
the existing local multilingual embedding model. The pack is returned with the claim and appended to each worker
prompt. A vector failure falls back to full-text search. A claim still succeeds if the knowledge store is
unavailable; in that case its pack is empty.

Every pack has a `knowledge_context_packs` row, even when it contains no items. Each delivered item is linked to
the run in `knowledge_deliveries`, and increments its handoff count once for that run. Retired records are excluded
from run packs and knowledge search. Stale records are flagged in the pack for review until an administrator
approves, corrects or retires them. A correction saves a revised item and retires the superseded record, so its old
text is no longer offered to runs or returned by knowledge search. Convention, skill, guide and decision items
untouched for 180 days are automatically flagged stale. A newer manual decision with the same title also marks its
active predecessor stale.

The content filter rejects manually submitted credentials and common direct contact identifiers such as email
addresses and phone numbers. Automatically captured source text is redacted before save so run and incident
records can still be retained without those values. The same filter protects the existing versioned Knowledge
resource form.

## Knowledge screen and API

Open **Knowledge** in the console. The Learning and study cases panel is scoped to the selected project plus
general records. It shows counts by project and kind, newest items, most-used items, item handoff counts, search,
source links, approver, context-pack coverage, repeated known mistakes and weekly verification rework rounds per
ticket. Its effectiveness figures and project/kind counts use the selected project's stored packs and runs plus
general records; another project's records and metrics are not included. Administrators can add or correct items,
approve or review them, flag them stale, merge duplicates of the same scope and kind, and retire items.

The console uses these endpoints:

- `GET /v1/knowledge/items?project=KEY&q=...` for scoped dashboard data and hybrid search.
- `POST /v1/knowledge/items` to add a project, epic or general item.
- `POST /v1/knowledge/items/{id}/correct` to save a corrected version, optionally changing its scope.
- `POST /v1/knowledge/items/{id}/approve`, `/stale`, `/retire` and `/merge` for moderation.

Read access uses `knowledge.read`; item changes require `knowledge.manage`. Search results for a project contain
only that project's records and general records. `known_mistakes_repeated` counts a case when its normalized cause
matches an earlier non-retired study case recorded in the same project or a general case already delivered to that
project. The current run is excluded; matching is project-wide and does not require the same ticket. Rework rounds
are verification runs with outcome `rework`, grouped by week; each row includes rounds divided by the distinct
ticket count for that week.

## Acceptance evidence

`test_first_project_failure_is_shared_to_second_project_run_and_avoided` demonstrates the two-project flow. An
ALPHA verification rework automatically records the nonce mistake. An administrator corrects the lesson into
general scope, which retires the original ALPHA record. The BRAVO run's stored context pack and worker prompt then
contain the prevention advice, and the run summary records nonce validation before accepting checkout.
`test_dashboard_scopes_counts_effectiveness_and_rework_to_project_plus_general` checks that ALPHA sees only its
own and general counts and metrics, including the weekly rounds-per-ticket calculation.

`uv run --frozen --with playwright==1.64.1 pytest tests/v4/test_knowledge_learning.py::test_knowledge_learning_screen_is_verified_in_a_browser -q`
launches Chromium, signs in, opens Knowledge, and checks project counts, newest and most-used items, a handed-to-run
count, search results, source and approver, administrator actions, context-pack and repeated-mistake figures, and
rework rounds per ticket. This is the browser verification for the Knowledge screen; Playwright is an ephemeral
test overlay and is not added to project dependencies.

## Data and migration

Migration `011_knowledge_learning.sql` creates the scoped item, context-pack and handoff tables. It stores
384-dimensional vectors in PostgreSQL using the existing pgvector extension and creates them locally through the
existing FastEmbed/ONNX setup; no new dependency or external embedding service is used. The prior memory search
remains available for tickets and run summaries. The Learning and study cases panel searches the new curated and
automatic knowledge records.
