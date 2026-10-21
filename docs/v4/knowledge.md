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
change the active resource snapshot while the request is pending. An owner uses the configured owner access token
on the Knowledge screen to approve and merge; the control repo cache is refreshed after merge so the policy is
used for subsequent decisions. Configure `AIFLOW4_OWNER_TOKEN` in the control API's secret environment; it is
separate from the reviewer token used by Human review.

The current console shares the trusted operator boundary used by the other v4 views and has no per-user sign-in.
For non-policy edits, the editor name is supplied in the form and recorded with the history entry. The owner token
authenticates policy approval; the owner name entered for the approval is recorded separately.

The control API exposes `GET /v1/knowledge/resources`, `POST /v1/knowledge/resources`,
`GET /v1/knowledge/resources/{type}/{id}/history`, and
`POST /v1/knowledge/resources/{type}/{id}/restore`. Policy proposals are listed with the resources and can be
approved through `POST /v1/knowledge/proposals/{proposal_id}/approve` with owner-token authorization.
