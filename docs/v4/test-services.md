# Test services of a project

**Console screens:** `projects`, `cc`, `machines`

A project declares the test services its checks need (`postgres`, `redis`) with **one** setting, `services`, in its
project file in aiflow-control:

```yaml
key: JAIF
services: [postgres, redis]
```

## One setting

* `databases` is the old name of the same setting. It is read as `services` when the project file is loaded
  (`Project.__post_init__` in `aiflow/v4/core/controlrepo.py`), so a file that lists `databases: [postgres, redis]`
  and no `services` makes the machines prepare and report both. A name that is no test service stays in
  `databases` and does nothing.
* The next save from the Projects screen (or from the flow doctor) writes `services` and removes `databases` from the
  file, so the migration is also visible in the file.

## Projects screen

The project list has a **Test services** column with one field per project (comma separated, `postgres, redis`) and a
Save button; the Add project form has the same field. `GET /v1/projects` includes each project's declared services;
`POST /v1/projects` saves the services selected while adding a project, and `POST /v1/projects/{key}/services`
updates them later (permission `projects.manage`, audited). Every machine of the project gets the
list in its next heartbeat answer (`desired.project_services`), starts the containers and reports them, so they are
ready within two minutes (heartbeats are every 30 seconds; the control repository is refreshed on save).

## Flow doctor: a park on an undeclared service

A ticket is parked when a run reports a missing service (`tool_waits`). If the service is `postgres` or `redis` and the
ticket's project does not declare it, no machine would ever prepare it. Each watchdog pass
(`tooling.declare_undeclared_services`):

1. declares the service for the project (the same write as the Projects screen), so the machines prepare and report
   it and the wait ends on their next heartbeats; or, when the file cannot be written (for example GitHub is not
   connected),
2. opens one `service_undeclared` alert per project, shown under Incidents, that names the service and says to add it
   under Test services on the Projects screen. The alert closes when the project declares it. See the
   [service_undeclared runbook](runbooks/service_undeclared.md).
