# Licensing and usage

Owners and Super Administrators open **Licence** in the console to view the installation ID, current plan, expiry,
resource limits and usage. They can activate online with a licence key or select a signed licence file for offline
activation. The key is sent only to the configured licence service; AI Flow does not save it. A rejected key or file
does not replace the current licence. The Licence screen remains available in read-only mode so an owner can renew.

## Issuer configuration and licence format

Set `AIFLOW4_LICENSE_PUBLIC_KEY` to the Ed25519 PEM public key used by the licence issuer. Online activation also
requires `AIFLOW4_LICENSE_SERVICE_URL`, the issuer's HTTPS activation endpoint. AI Flow sends one JSON request with
exactly `key` and `installation_id`. The endpoint responds with `{"license_token":"<signed-token>"}`. The token is
a compact EdDSA JWT with `plan` (`free`, `team` or `business`), integer `exp` (Unix seconds), and the matching
`installation_id` claims. AI Flow verifies the signature and claims locally before saving it. Redirects are not
followed. The key and token are never written to application logs.

For a closed network, request a signed licence file for the installation ID shown on the Licence screen. The file
contains the same compact JWT as a single line of text. The control plane verifies it locally and makes no network
request. Files for another installation, expired files, unsupported plans, malformed tokens and bad signatures are
rejected.

## Plans and limits

The initial Free limits are 3 machines, 6 AI accounts, 3 projects and 3 users. Team starts at 5, 12, 25 and 25;
Business starts at 50, 100, 250 and 250. An owner can edit each plan's limits on the Licence screen. The current
usage counts are registered machines, configured AI accounts, configured projects and enabled console users.

Creating a project, registering a new machine, adding a console user, or starting new work on an excess machine,
project or AI account returns a message naming the limit and the next step. Owners and Super Administrators create
console users from **Users and roles**; the generated initial password is returned once for secure sharing. Existing
running work continues. An installation with a licence in its grace period continues to work under its plan limits.

## Expiry and read-only mode

The installation warns owners during a 14-day grace period. For an expired licence, grace starts at the signed
expiry time. If a saved licence fails signature or claim verification, grace starts the first time the control plane
detects that failure. AI Flow continues to run during grace. When grace ends, console changes and new work or
resource creation are refused with a read-only message. Existing runs may report results and release leases, and
licensing activation and renewal remain available. No licence transition deletes projects, tickets, runs, users or
other data. A valid activation restores normal operation.

## Usage report

The Licence screen reports tickets whose current status is **Done**, grouped by project and the UTC month in
`done_at`. AI input/output tokens and cost are grouped separately by project and the UTC month in `runs.started_at`.
The **Export spreadsheet** action downloads a UTF-8 CSV that opens in common spreadsheet applications. The report
reads the existing ticket and run records and does not duplicate or discard usage data.

## No licence configured, over-limit grace and alerts

When neither an issuer public key (`AIFLOW4_LICENSE_PUBLIC_KEY`) nor a stored licence exists (self-hosted,
development, a lab), nothing can be licensed, so the installation runs **without any licence limit or grace
period** and the Licence screen shows "No licence required". Limits apply as soon as an issuer key is configured.

The Free plan defaults are 3 machines, 6 AI accounts, 3 projects and 3 users, enough to evaluate the product on one
project; the owner can edit them on the Licence screen. Installations that still carry the old Free defaults
(1 machine, 2 AI accounts) are updated by the migration; edited limits are kept.

When a limit is newly applied or lowered below current usage, the first time usage is found above it starts the
existing 14-day grace period (recorded once as `over_limit_since`, cleared when usage is back within every limit).
During it, machines, AI accounts and projects already over the limit keep working and keep getting work; only
creating new ones is refused. If usage is still over the limit when the grace period ends, the limit stops new work
for the extra ones. That stop opens a **critical `licence_limit` incident** at once, naming the limit, the usage and
the action (raise the limit in the Licence screen or remove the extra ones), and alerts the owner's channels. The
incident closes by itself when work resumes (usage within the limit, limit raised, or grace applies). The idle-flow
alert names the licence limit as the cause when ready tickets get no worker because of it.
