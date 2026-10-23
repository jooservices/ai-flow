# Team Lead requirement coverage and progress reports

Open `/lead` to select a project and epic. The view lists descendant tickets with their parent, status, Acceptance criteria lines and the latest completed verify result for each criterion. The report flags an epic with no child tickets, a ticket with no AC lines in its `Acceptance criteria` section, and a Done ticket whose latest completed verify run did not PASS.

Verify runs in v4 record one ticket-level verdict. A successfully merged or human-review verify run confirms every AC line parsed from that section. Rework, failed, blocked, missing or unfinished verify runs leave the ACs unconfirmed, including when the verifier passed but CI failed afterwards. The display does not infer individual AC failures from free-form findings. A ticket's latest completed verify result is used, even if Jira was moved to Done manually afterwards.

Epic progress reports include Done/total, In Progress/In Verify/Human review, current Needs owner tickets, verify rework rounds, lead-time statistics, recorded tokens and estimated USD cost. Lead time is measured from Jira creation to completion for Done tickets and reported as median and p90. Token and cost values sum all recorded runs for tickets under the epic.

`GET /v1/projects/{project}/report?epic={epic}` returns the structured report. Set `format=md` or `format=csv` to download an export, or `format=html` for a standalone report with print styles; use the browser's Print command to print or save it as PDF. CSV has one row per ticket, with its AC descriptions and verify states grouped in a quoted cell. The progress totals are repeated per ticket so each exported row carries the epic context.

Coverage gap reason codes in JSON and exports are `epic_has_no_child_tickets`, `ticket_has_no_acceptance_criteria`, and `done_without_verified_acceptance_criteria`.
