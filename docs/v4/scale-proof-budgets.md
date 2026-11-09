# Scale-sensitive read paths

**Console screens:** `cc`, Projects (`projects`)

## Request data

`GET /v1/state` uses `release_progress._load` to collect release progress. `_release_steps_many` reads release audits,
check events and run nodes once for the complete set of release trackers, then builds each tracker's steps in the
existing order. Add new release-step data to that batch so the number of database reads stays fixed as versions grow.

`GET /v1/projects` calls `projects.service.managed_project_list`. Its `projects.repository.listing_data` query reads
ticket totals, machine readiness and project check probes together. The service keeps the existing project fields and
sort order while reusing those rows in the readiness and probe formatters. Add listing data in the repository result
and pass it to the relevant formatter instead of adding a read inside the project loop.

## Routes

- `GET /v1/state` Returns the permission-filtered Command Center, Delivery and SA snapshot.
- `GET /v1/projects` Lists the projects visible to the signed-in user with ticket counts, machine readiness and check results.
- `GET /v1/business/{key}/release-progress` Returns a project's versions with ticket progress, readiness reasons, release steps and scale-proof results.
- `GET /v1/deploy/state` Returns the deployment snapshot used by authenticated control-machine deployment tooling.
