# Release readiness of a Jira version

AI Flow shows, for every unreleased Jira version of a configured project, whether the version is ready to release
and, if not, why. The result is computed from the registry on every read (`aiflow/v4/control/release_readiness.py`);
nothing is stored. It only reports: the release flow, tags and deploys in [release.md](release.md) and
[version-release.md](version-release.md) are unchanged.

## Rule

Membership is the set of tickets whose Jira Fix versions currently include the version, as mirrored by Jira sync
([jira-versions.md](jira-versions.md)). A version is ready when it has at least one ticket and every one of them:

| Condition | Reason when it fails (`cause`) |
|---|---|
| Registry status is `Done` | `not_done`, with the current `status` |
| No `rework` label, and its latest finished verify run did not end in `rework` | `rework` |
| Its latest finished verify run passed (outcome `merged`/`human`, or verdict `PASS`); checked for Done tickets | `not_verified` |
| No open (not Done) ticket of type `Bug` with label `release-qa` is in its `blocked_by` links | `blocked_by_bug`, with the Bug's key in `bug` |

A version with no ticket is never ready; its only reason is `empty`.

Adding a ticket to the version in Jira (for example two tickets added to a version of ten Done tickets) makes it not
ready at the next sync until the new tickets also meet the rule. Removing a ticket from the version recomputes it:
if the removed ticket was the only blocker, the version becomes ready.

## Where it shows

- `GET /v1/state` has `release_readiness`: one entry per unreleased version with `project`, `version_id`,
  `version`, `ready`, `ticket_count`, `done_count`, `tickets` and `reasons`. Each reason has `cause`, `ticket`,
  `summary` and, by cause, `status` or `bug`.
- **Command Center** (`/cc`) has a *Release readiness* panel listing every project's versions and their reasons.
- **Business** (`/business`) has a *Sẵn sàng release* panel for the chosen project, with the same reasons in
  Vietnamese. `GET /v1/business/{project}/release-readiness` returns the same entries for one project.
