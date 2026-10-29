# Needs owner triage

A developer AI that stops with a question parks the ticket in **Needs owner**. Accounts with the `master` role answer
those questions so that only real requirement conflicts and business decisions reach the owner. After the one-time
legacy inventory import, account roles are stored in the control database and managed in **Machines and AI**; the
control plane does not read `accounts.yaml` for roles again.

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
- `DECISION: ESCALATE` plus why the owner must decide: AI Flow posts a Jira comment that starts with the question,
  lists the options and recommended option, adds `needs-owner` (and the compatibility label `owner-escalated`),
  and leaves the ticket in **Needs owner**. An optional notification contains only the question and ticket link;
  grouped questions share the one-hour delivery limit. Run outcome `escalated`.

Anything else (no decision, an empty answer or reason) is recorded as `failed` and performs no Jira write. If a
required Jira write during `ESCALATE` fails, the escalation remains eligible for triage, which retries the pending
decision and reuses the question comment if Jira already recorded it.

Since JAIF-255 the decision goes through the owner decision register and the escalation classes
([escalations](escalations.md)): `DECISION: ESCALATE` needs a class (a new business decision or an irreversible or
production action), at least two options and a recommended default; a decision the owner already made is applied and
cited instead; a new kind of decision is asked once; `DECISION: AMEND` narrows an acceptance criterion that cannot be
met here.

## Owner hand-off

Triage runs are recorded in the `runs` table with step `triage` and appear in the console's Runs table. The owner
answers by commenting in Jira and moving the ticket to **To Do**. Before a development claim, control verifies that
the connected Jira owner authored a comment after the timestamp of the actual question comment. Removing a label or
moving the ticket to To Do alone does not resume development; without a verified answer the claim is refused and the
ticket is returned to **Needs owner**. After a verified answer, AI Flow clears the owner labels and passes the answer
to development, including other tickets grouped under the same decision. The Command Center cannot answer a
ticket-linked question or resume it without the verified Jira comment.
