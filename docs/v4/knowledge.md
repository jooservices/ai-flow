# Knowledge and Skills screen

Open **Knowledge** in the console or visit `/knowledge`. The screen lists and edits study cases, knowledge notes,
skills, agents, guides and policies stored in `aiflow-control`. Entries can be created, edited, archived,
filtered by type, and restored from their version history. Each version records the editor name, save time, action,
commit or pull request, and full content snapshot; the history view shows the prior and new content together.

Non-policy resources are committed by the GitHub App bot to the `aiflow-control` default branch. Their Markdown
files use YAML front matter for resource identity, title and archive state. Skills are stored as
`skills/<id>/SKILL.md`; other notes use `study-cases/<id>.md`, `knowledge/<id>.md`, `agents/<id>.md` and
`guides/<id>.md`.

Global policy `default` is stored in `policy/default.yaml`. A project policy uses its uppercase project key as the
resource id and is stored in `policy/projects/<PROJECT>.yaml`; `ControlRepo.policy_for()` applies that policy to
the project's decisions. Saving, archiving or restoring a policy creates a branch and pull request. It does not
change the active resource snapshot while the request is pending. A `super_admin` or `owner` can approve and merge
from the Knowledge screen using their signed console session. The configured owner access token remains available
for approval calls that do not use a console session. The control repo cache is refreshed after merge so the policy
is used for subsequent decisions. Configure `AIFLOW4_OWNER_TOKEN` in the control API's secret environment; it is
separate from the reviewer token used by Human review.

The `super_admin` and `owner` roles can create, edit, archive and restore resources and approve policy proposals.
`lead` can view resources and history but cannot make changes. The console uses the permissions returned by
`GET /v1/console/session`; the API checks the same role map before reading or changing resources. See
[Console roles and permissions](console-roles.md) for the full UI and API permission mapping. For non-policy edits,
the editor name supplied in the form is recorded with the resource history; the session permission controls whether
the write is allowed.

The control API exposes `GET /v1/knowledge/resources`, `POST /v1/knowledge/resources`,
`GET /v1/knowledge/resources/{type}/{id}/history`, and
`POST /v1/knowledge/resources/{type}/{id}/restore`. Policy proposals are listed with the resources and can be
approved through `POST /v1/knowledge/proposals/{proposal_id}/approve` with an authorized console session or the
configured owner bearer token.
