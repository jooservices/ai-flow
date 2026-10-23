# Needs owner triage

A developer AI that stops with a question parks the ticket in **Needs owner**. Accounts with the `master` role in
`aiflow-control/accounts.yaml` (Claude and Codex both hold it) answer those questions so that only real
requirement conflicts and business decisions reach the owner.

## Candidates

- Tickets in Needs owner without the `owner-escalated` label are offered as step `triage` to accounts with the
  `master` role. The account of the last dev run on the ticket is never offered it.
- Verify and triage candidates come before new development work.
- A triage run that fails, times out or returns no valid decision backs off like dev and verify (2, 4, 8, 16 then
  30 minutes), so a broken ticket does not starve development.

## The run

The claim carries the ticket, the last dev run summary (the question), the latest verify findings and the recent
Jira comments. The worker runs `prompts/v4/triage.md` in a read-only, detached checkout of the project's base
branch and removes the checkout afterwards. The AI must end with one of:

- `DECISION: ANSWER` plus the answer: AI Flow posts a templated comment (What, Why, Result, Next) and moves the
  ticket to **To Do**. Run outcome `answered`. The next dev run gets the question and the answer in its prompt,
  so it continues instead of stopping again.
- `DECISION: ESCALATE` plus why the owner must decide: AI Flow posts the same comment shape, adds
  `owner-escalated` and sends an alert through notify (`POST /v1/notify/triage`). The alert is critical when
  another ticket is blocked by this one. Run outcome `escalated`.

Anything else (no decision, an empty answer or reason) is recorded as `failed` and changes nothing in Jira.

Since JAIF-255 the decision goes through the owner decision register and the escalation classes
([escalations](escalations.md)): `DECISION: ESCALATE` needs a class (a new business decision or an irreversible or
production action), at least two options and a recommended default; a decision the owner already made is applied and
cited instead; a new kind of decision is asked once; `DECISION: AMEND` narrows an acceptance criterion that cannot be
met here.

## Operators

Triage runs are recorded in the `runs` table with step `triage` and appear in the console's Runs table. To resume
an escalated ticket, record the decision on the ticket and remove the `owner-escalated` label (or move it to
To Do yourself).
