# Node administration repository

The fleet feature owns the named active-node lookup used by node administration. `ApplicationContext.node_repository`
provides a `NodeRepository` by default; tests or callers can inject a `FakeNodeRepository` initialized with active names.
The real query keeps the exact node-name equality parameter and ignores soft-removed rows. Add later node persistence
operations to this repository as their owning tickets move the SQL out of routes, keeping unrelated writes and query
order unchanged.

## Routes

- `PUT /v1/nodes/{name}/work-acceptance` enables or stops a registered node from accepting new work.
- `DELETE /v1/nodes/{name}` removes a registered node after confirmation when it has no running sessions.
