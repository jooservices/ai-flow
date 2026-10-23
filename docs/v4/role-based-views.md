# Role-based console views

The console uses `ROLE_PERMISSIONS` in `aiflow/v4/control/console_auth.py` as the single role-to-permission table.
The browser receives the signed-in person's effective permissions from `GET /v1/console/session` and builds its
menu from those grants. The API applies the same grants to direct view URLs and view-specific endpoints. A person
with multiple roles receives the union of their views.

| Role | Views | Sign-in landing page |
|---|---|---|
| Administrator | Every view, including Users and roles and Projects | Dashboard |
| Product owner | Dashboard, Work board, Delivery, Business, Team Lead, Stakeholder, Human review, Knowledge | Dashboard |
| Delivery | Dashboard, Work board, Delivery, Human review | Delivery |
| SA | Dashboard, Work board, SA, Machines and AI, Integrations, Knowledge | SA |
| BA | Dashboard, Work board, Business, Knowledge | Business |
| Team Lead | Dashboard, Work board, Delivery, Team Lead, Human review, Knowledge | Team Lead |
| Business | Dashboard, Work board, Business | Business |
| Reviewer | Dashboard, Work board, Human review | Human review |
| Stakeholder | Dashboard, Work board, Stakeholder | Stakeholder |

The landing page for a person with multiple roles uses this priority: Administrator, Product owner, Delivery, Team
Lead, SA, BA, Business, Reviewer, Stakeholder. Dashboard and Work board are available to every role. Both use
only projects assigned to the signed-in person; the same project scope applies to project-specific API reads.
An unassigned project request is refused and recorded in `write_audit`.

Opening a view or API that the person's effective roles do not grant returns HTTP 403 with `not available for your
role`. Direct view URLs return a clear refusal page. Each refusal records the actor, path and effective role in
`write_audit`.

## Manage people

Administrators open **Users and roles** in the console to add a person, choose one or more roles, set project access,
change a person's roles or projects, and disable or re-enable the account. Adding a person creates a one-time
password shown to the administrator once. Role, project and account changes include the administrator in the audit
record and appear in the change history. The final active Administrator cannot lose the Administrator role or be
disabled.

An Administrator can choose **View as** in the console to check one role's exact menu and API permissions without
signing out. The view-as state is signed by the server and enforced by API checks; **Return to my role** restores the
Administrator's full access.

Human review actions (approve, rework, reject, ask the AI, answer, skip the gate) need the `review.actions`
permission, held by Administrator, Product owner, Delivery, Team Lead and Reviewer. A signed-in person with that
permission acts with their own session, inside their project limits; the shared reviewer bearer token still works for
automation. The master autonomy report (`/v1/master/autonomy`, `/v1/master/report`) lists only the tickets of the
person's projects and omits the shared AI-account quota section for a project-limited person.

The first user installed with the existing Super Administrator assignment is stored as Administrator and shown
with that name. Existing `owner` assignments migrate to Administrator and `lead` assignments migrate to Team Lead.
The API accepts `super_admin`, `owner` and `lead` as legacy role names for one release, normalizing them to their
new roles.


## Route permissions

Each route below is guarded by the permission in the second column; a role without it is refused with
"not available for your role" and the refusal is audited. A person limited to projects only receives the
rows of those projects.

| Route | Permission |
|---|---|
| POST `/v1/accounts/{account_id}/sign-in/complete` | `view.machines_ai` |
| GET `/v1/accounts/{account_id}/sign-in` | `view.machines_ai` |
| POST `/v1/accounts/{account_id}/sign-in` | `view.machines_ai` |
| DELETE `/v1/accounts/{account_id}` | `view.machines_ai` |
| PATCH `/v1/accounts/{account_id}` | `view.machines_ai` |
| GET `/v1/analytics/daily` | `view.dashboard` |
| GET `/v1/analytics/usage` | `view.dashboard` |
| GET `/v1/auth/users` | `console.manage` |
| POST `/v1/auth/users` | `console.manage` |
| PUT `/v1/backups/config` | `backup.read` |
| POST `/v1/backups/key` | `backup.read` |
| POST `/v1/backups/run` | `backup.read` |
| GET `/v1/backups` | `backup.read` |
| PATCH `/v1/console/users/{email}/disabled` | `console.manage` |
| PUT `/v1/console/users/{email}` | `console.manage` |
| GET `/v1/console/users` | `console.manage` |
| POST `/v1/console/users` | `console.manage` |
| POST `/v1/console/view-as/stop` | `console.manage` |
| POST `/v1/console/view-as` | `console.manage` |
| POST `/v1/integrations/github/imports/{pending_id}/connect` | `view.integrations` |
| GET `/v1/integrations/github/imports/{pending_id}/install` | `view.integrations` |
| GET `/v1/integrations/github/imports/{pending_id}/installations` | `view.integrations` |
| POST `/v1/integrations/github/imports` | `view.integrations` |
| GET `/v1/integrations/github/installation-settings` | `view.integrations` |
| GET `/v1/integrations/github/manifest/start` | `view.integrations` |
| GET `/v1/integrations/github` | `view.integrations` |
| POST `/v1/integrations/jira/api-token` | `view.integrations` |
| POST `/v1/integrations/jira/disconnect` | `view.integrations` |
| POST `/v1/integrations/jira/oauth/start` | `view.integrations` |
| GET `/v1/integrations/jira` | `view.integrations` |
| POST `/v1/knowledge/items/{item_id}/{action}` | `view.knowledge` |
| GET `/v1/knowledge/items` | `view.knowledge` |
| POST `/v1/knowledge/items` | `view.knowledge` |
| GET `/v1/knowledge/resources/{kind}/{resource_id}/history` | `view.knowledge` |
| POST `/v1/knowledge/resources/{kind}/{resource_id}/restore` | `view.knowledge` |
| GET `/v1/knowledge/resources` | `view.knowledge` |
| POST `/v1/knowledge/resources` | `view.knowledge` |
| POST `/v1/licensing/activate` | `licensing.manage` |
| PUT `/v1/licensing/limits` | `licensing.manage` |
| POST `/v1/licensing/offline` | `licensing.manage` |
| GET `/v1/licensing/status` | `licensing.manage` |
| GET `/v1/licensing/usage.csv` | `licensing.manage` |
| GET `/v1/licensing/usage` | `licensing.manage` |
| GET `/v1/licensing` | `licensing.manage` |
| POST `/v1/machines/{name}/accounts` | `view.machines_ai` |
| PUT `/v1/master/autonomy/budget` | `console.manage` |
| GET `/v1/master/autonomy` | `view.machines_ai` |
| GET `/v1/master/report` | `view.machines_ai` |
| GET `/v1/memory/search` | `view.knowledge` |
| GET `/v1/memory/study-cases` | `view.knowledge` |
| POST `/v1/nodes/{name}/join-command` | `view.machines_ai` |
| GET `/v1/nodes/{name}/operations` | `view.machines_ai` |
| POST `/v1/nodes/{name}/operations` | `view.machines_ai` |
| PUT `/v1/nodes/{name}/work-acceptance` | `view.machines_ai` |
| DELETE `/v1/nodes/{name}` | `view.machines_ai` |
| GET `/v1/projects/catalog` | `projects.manage` |
| POST `/v1/projects/preflight` | `projects.manage` |
| POST `/v1/projects/{key}/pause` | `projects.manage` |
| GET `/v1/projects/{key}/repositories` | `projects.manage` |
| PUT `/v1/projects/{key}/repositories` | `projects.manage` |
| DELETE `/v1/projects/{key}` | `projects.manage` |
| GET `/v1/projects` | `projects.manage` |
| POST `/v1/projects` | `projects.manage` |
| GET `/v1/reviews/export.csv` | `view.human_review` |
| GET `/v1/reviews` | `view.human_review` |
| PUT `/v1/role-health/settings` | `console.manage` |
| GET `/v1/state` | `view.dashboard` |
