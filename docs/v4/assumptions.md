# AI assumptions and technical choices

**Console screens:** `cc`

The owner is a person who defines business requirements. The owner must not be asked questions about code structure,
naming, scope, metrics, defaults or tools. When the intake AI or developer AI is unsure about any of these aspects,
it does not stop or escalate to the owner. Instead, it decides conservatively in accordance with `docs/v4/architecture.md`,
project rules, and the parent epic.

The chosen option is recorded under an `Assumptions:` section in the ticket description, detailing both what was assumed
and why. The ticket proceeds without stopping in Needs owner.

Verification checks the recorded assumptions against project rules, the parent epic, and architecture guidelines.
Verification sends a ticket back for rework (`VERDICT: FAIL`) only if an assumption contradicts a rule, and never asks
a person. When rework is triggered due to a contradiction, only the assumptions the verifier names in its findings
(`- assumption: <text> contradicts <rule>: <why>`) are marked as contradicted. Recording is idempotent: an assumption with the
same text on the same ticket is stored once, and a run result with an invalid assumption item is refused (HTTP 422) so the
worker can correct it. The dashboard panel only shows the projects of the signed-in user and counts whole UTC days.
An intake question that is technical is answered by the flow at most twice per ticket; after that it reaches the owner path.

Both intake and development read the parent epic rules alongside the child ticket, ensuring rules defined once on an
epic automatically apply to all child tickets under it.

Only business questions (customer-facing requirements, pricing, licensing, legal) and irreversible actions are permitted
to escalate to a human owner. Only tickets waiting on true business decisions count toward the "Needs owner" count on the
Command Center dashboard.

The Command Center dashboard includes an "AI assumptions" panel displaying assumptions recorded per UTC day and tracking
the number of assumptions later contradicted during verification.

## Routes

- `GET /v1/dashboard/assumptions` returns assumptions made per day and how many of them were later contradicted.
- `GET /v1/reviews` lists the tickets waiting for a person; a ticket in Needs owner appears only when it waits for a business decision or an irreversible action.
- `POST /v1/runs/{run_id}/result` accepts the run's result; a verify rework whose findings say an assumption contradicts a rule marks that assumption as contradicted.
- `POST /v1/work/claim` hands out the next piece of work; the ticket payload now carries the parent epic so the AI applies the epic's rules.
- `GET /v1/projects/{key}/report` and `POST /v1/projects/{key}/close` read and close a project report; the report counts only Needs-owner tickets that wait for a business decision.
- `POST /v1/jira/{key}/description` replaces a ticket description; an `Assumptions:` section in the new text is recorded as assumptions of that ticket.
- `GET /v1/state` and `GET /v1/deploy/state` return the dashboard state; each ticket says whether it is a business Needs-owner ticket.
