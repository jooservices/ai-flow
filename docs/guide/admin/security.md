# Administrator guide: security

**Assumes:** you are a Super Administrator, console login is on (the installer turns it on), and AI Flow v4 is
installed. This page describes what the product does today; it is not a certification.

## Who can do what

| Role | May |
|---|---|
| Super Administrator | everything |
| Owner | read the console; manage integrations, nodes, projects and knowledge; approve policies |
| Lead | read the console, node history and knowledge |

The console hides what a role may not do, and the API checks the same permission map again. Console pages without
a session redirect to the sign-in page; every other path answers 401.

## Sessions and passwords

Sign-in uses email and password. The session cookie is `HttpOnly`, `SameSite=Strict` and `Secure` behind HTTPS,
and lasts 12 hours. Sign-in attempts are rate limited per client address and per address and email. Passwords
are stored hashed (scrypt).

## Secrets

- Jira, GitHub and AI credentials stay on the control machine or the node under `/etc/aiflow` and the secret directories. The console, logs and these documents never show them.
- Do not paste a token into a ticket, a chat or a support request.
- Nodes, workers and reviewers each use their own bearer token. Rotate a token by issuing a new one and restarting the unit that uses it.

## Releases

Every release tarball and control image is checksummed and signed with cosign. The installer and the nodes verify
both before they install; a node that refuses a release raises a `release_verification` incident
([runbook](../runbooks/release_verification.md)).

## Network

Put TLS in front (Traefik serves HTTP on port 80), restrict the origin to your proxy, and keep PostgreSQL and
Redis reachable only on the internal network, as the installer sets them up.
