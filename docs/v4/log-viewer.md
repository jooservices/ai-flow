# Host and service log viewer

**Assumes:** AI Flow v4 is installed, a node is registered, and the person has an authenticated Command Center session.

The administrator opens **Host/service logs** from the Command Center. The screen lists named sources for every
registered node and can request a snapshot of the latest 100 lines from one source. It does not follow new lines
live. Reads run through the node agent with fixed argument lists; the screen has no command or file-path input.

## Access

Only the `administrator` role can open `/logs` or use the log-source and log-request APIs. The `owner` and
`super_admin` role aliases normalize to `administrator`. Other roles, including SA and Team Lead, cannot see the
navigation link and receive an authorization refusal from both the screen route and APIs. An authenticated
session is required even when the general console login gate is disabled.

## Listed sources

The source inventory is created by the control API from registered, non-removed nodes:

- Each node has a **Host journal** source, which reads the most recent entries from that host's system journal.
- Each node has its `aiflow4-node-agent.service` systemd journal.
- Each node has a systemd journal source for every configured AI account assigned to it (`aiflow4-worker@<account>.service`).
- A node with the `control` role also lists the fixed containers from the v4 Compose files: `aiflow4-control-api-a`,
  `aiflow4-control-api-b`, `aiflow4-control-loops`, `aiflow4-postgres`, `aiflow4-redis`, `aiflow4-traefik`,
  `aiflow4-openobserve`, `aiflow4-gatus` and `aiflow4-svc-mailpit`.

The APIs accept only source identifiers from this inventory. The node agent independently accepts only the host
journal, the named AI Flow systemd units and those fixed container names. It does not accept an arbitrary file,
unit or container name. Offline nodes are listed as unavailable; a source that is missing on an online node is
also reported as unavailable after the read attempt.

## Redaction and empty results

Before a node result is stored or returned, the control API reuses the credential sanitizer from
`control/support_bundle.py`. Password, secret, token, API key and similar assignments, bearer credentials, known
token formats, private-key headers and URL credentials are replaced with the fixed marker `[REDACTED]`. The same
sanitizer also removes recognizable personal identifiers. Results are bounded to 100 lines and 4,000 characters
per line, and log output is excluded from the general node-operation API. Read results are available to the
administrator for 15 minutes so the screen can poll an in-progress node request.

The screen distinguishes an unavailable source from a source with no recent lines. Both states are shown as
plain messages, without a traceback.
