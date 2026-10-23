# Release progress screen

The Command Center (`/cc`) and Business (`/business`) show where every Jira version is: Done tickets of the total,
open tickets, tickets added late, why the version is not ready, and the state of the release flow. Everything is read
from the registry on each request (`aiflow/v4/control/release_progress.py`); the screens are read-only.

## Data

- **Versions and tickets**: Jira Fix versions mirrored by sync ([jira-versions.md](jira-versions.md)). Unreleased
  versions first, then the 10 most recent released ones per project. The total counts every ticket now in the version.
- **Added late**: a ticket is `added_late` (with `added_at`) when its Fix version was attached after the first ticket
  of the version moved to In Progress. Sync reads each issue with its Jira change history and keeps the attach date
  in `ticket_version_added` and the first In Progress move in `tickets.first_in_progress_at` (migration 009). A ticket
  without history is never marked late.
- **Reasons not ready**: the conditions of [release-readiness.md](release-readiness.md) (`not_done` listing keys,
  `rework`, `not_verified`, `blocked_by_bug`, `empty`) plus `release_running` / `release_failed` from the release
  flow row (`version_releases`, with its detail). A released version has none.
- **Release flow** ([version-release.md](version-release.md)): `release` has `status`, `tracker`, `tag`,
  `github_release_url` and `steps` (check, changelog, pr, merge, tag, release), each with `state`
  (`done`, `running`, `failed`, `pending`), `run_id`, `node` (from the run row), `detail` and `url`.

## API

- `GET /v1/state` has `release_progress`, one entry per version (all configured projects).
- `GET /v1/business/{project}/release-progress` returns `{project, versions}` for one project (404 for an unknown
  project).

## Screens

- **Command Center**: *Release progress* panel with x/y Done, late tickets and their date, open tickets with status,
  reasons, and the release steps with run ID and node. Released versions show the tag and the GitHub Release link.
- **Business** (Vietnamese): *Tiến độ release* with x/y Done, late tickets, reasons, and for released versions the tag
  and GitHub Release link. Steps, run IDs and nodes stay in the Command Center.

The earlier readiness API (`release_readiness` in `/v1/state`, `/v1/business/{project}/release-readiness`) is unchanged.
