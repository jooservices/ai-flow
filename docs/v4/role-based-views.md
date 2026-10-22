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

The first user installed with the existing Super Administrator assignment is stored as Administrator and shown
with that name. Existing `owner` assignments migrate to Administrator and `lead` assignments migrate to Team Lead.
The API accepts `super_admin`, `owner` and `lead` as legacy role names for one release, normalizing them to their
new roles.

