# Support bundles

Owners and Super Administrators can open **Support** in the v4 console and build a diagnostic bundle. The bundle is
created in memory, scanned, and staged in shared Redis for ten minutes. The screen shows its file inventory only after the scan passes;
the owner can then download and inspect the ZIP before sharing it.

The general bundle includes the AI Flow, Python and node release versions; selected non-secret control settings;
machine and account states; up to 100 incidents opened in the last seven days; and control-service logs from the last
24 hours. Incident subject, title and detail are free-form text, so they are replaced with redaction markers before
scanning; the structured incident metadata remains available. Log messages are free text too (a lowercase name such as
"jane doe called support" cannot be found by patterns), so every log line keeps only its timestamp, level, and
allow-listed diagnostic `key=value` pairs whose value is a number, boolean or ticket key. Logger names and message
text are omitted; free-form message text is replaced with `[REDACTED_FREEFORM]`. Persistent logs use generic section
separators generated internally, while separator-shaped text from a log message is treated as free form. The result is
still scrubbed for credentials (including `password`, `pwd`, `pass` and `pin` style fields) and scanned again.
If the scan still finds a secret, token, key, email, phone number, IP address, or name-like personal identifier, the
bundle is rejected and no download is made available. Structured JSON is scanned for name-like identifiers too;
only trusted technical labels are retained in the known machine and account label fields (machine names, node
references and account ids): each hyphen- or underscore-separated segment must be a known technical word or a number
next to one (`wsl-1`, `claude-01`); a compact digit suffix is trusted only when its full alphabetic stem is a known
technical word (`node2`). Name-shaped labels such as `jane2`, `jane-doe` or `jane.doe` are replaced with a redaction
marker, and a label that would be offered unredacted fails the scan.
Secret values and free-form account/provider error state are not selected from settings or registry records.

Control API and loop processes write rotated logs to `/srv/aiflow/control/logs` in the standard Compose deployment.
Only entries with timestamps in the preceding 24 hours are included. Other deployments retain a bounded in-memory
log ring for the running API process. Each included log file is capped at 5 MiB.

Node release strings are included only when they match the v4 release format; other values appear as `unknown`.
Heartbeat tool reports are reduced to a version parsed from a recognized CLI version line, or the fixed status
`missing`, `timeout` or `broken`. Unknown tool names are omitted and unrecognized values are redacted.

To create a ticket bundle, select a ticket before building. It adds the ticket's key, project, status and type (the summary is free text and is replaced with a redaction marker)
and any run logs
from the preceding 24 hours. Workers attach the newest 5 MiB of a run log to that run's result, so the control plane
can collect logs from remote nodes. The completed archive is scanned with the same gate as the general bundle, and
every log file remains capped at 5 MiB.

Run logs are sanitized, capped and scanned when a worker reports its result, and kept only in Redis for about 25
hours; they are never written to the `runs` table or its backups. A log that still fails the scan is dropped.

Bundles are temporary and belong to the console user who created them: the creator is saved with the staged bundle
and a download by any other user is refused. Creating a bundle returns its inventory; the archive
is served only by the separate download action after the inventory has been shown in the Support screen. The bundle
is not uploaded or sent by AI Flow.

The Support screen uses `POST /v1/support/bundles` to create and scan a bundle and return its inventory, then
`GET /v1/support/bundles/{bundle_id}/download` to download it after review. Workers submit run results through
`POST /v1/runs/{run_id}/result`; the control service stores any sanitized support log in temporary storage for
ticket-specific bundles.
