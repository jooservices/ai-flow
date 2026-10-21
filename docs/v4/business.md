# Business view (`/business`)

The Business view is a third tab of the console, next to Command Center (`/cc`) and Delivery (`/delivery`). It is
for people who follow the work by epic, answer questions, and ask for changes. Its labels are in Vietnamese.

## Access

`GET /business` serves the same page (`aiflow/v4/control/ui/console.html`) with the same access as `/cc` and
`/delivery`: there is no login, no role and no node token. The Traefik router of the control API forwards
`/business` like the other console paths. Signing in by role is a separate ticket.

## What the screen shows

Pick a project with the project tabs. Every panel shows only that project.

- **Epics**: the progress of the work under each epic. The Delivery tab and the Business tab use the same
  function (`epicProgress` in the console script) on the same data (`/v1/state`), so the numbers are the same:
  items under the epic (sub-tasks count for their parent's epic), done, in verify or review, in progress, needs
  owner, not started, and the percentage done. Clicking an epic opens the read-only ticket drawer.
- **Cần bạn trả lời** (needs your answer): every ticket of the project in the status `Needs owner`, and only those.
  These are the intake's questions, developer questions, and tickets the triage escalated to the owner (label
  `owner-escalated`, shown as "Triage escalate"). Each row shows the key, the title, the epic and a link to Jira.
  The answer is written in Jira.
- **Gửi change request** (send a change request): a form with a title, a description and an epic (a select with the
  epics of the chosen project; "no epic" is allowed). Without a title or a description the form does not send and
  shows the error. After sending, the form shows the key and the Jira link of the new ticket.
- **Change request**: the project's tickets labelled `change-request`, newest first, with their impact assessment
  (size, affected epics and tickets, risks, conflicts with work under way) or "Đang đánh giá" (being assessed)
  while there is none yet. The list refreshes with every poll.

The screen has no action that changes a ticket's status.

## Change request flow

1. `POST /v1/business/<project>/change-requests` with `{"title", "description", "epic"}` (same access as the
   console). The API refuses (400) a missing title or description and an epic that is not an epic of the project;
   an unknown project is 404.
2. The control API creates exactly one Jira issue through the Jira gateway (`Jira.create_issue`): type Story, label
   `change-request`, the chosen epic as `parent`. A new issue that does not start in Backlog is moved to Backlog.
   The write is recorded in `write_audit` (target `jira`, action `create`, actor `business-console`), the ticket is
   mirrored in the registry at once, and the event `business.change_request` is emitted.
3. An `intake_refiner` account takes the ticket as the step **impact** (`aiflow/v4/worker/impact.py`, prompt
   `prompts/v4/impact.md`): tickets in Backlog with the label `change-request`, not on hold, not assessed yet. A
   failed try waits 10 minutes, like intake.
4. The AI reads the request, the project's open work (epics first) and `docs/SPEC.md` on the base branch, and
   answers with `SIZE` (S, M or L), `EPICS`, `TICKETS`, `RISKS`, `CONFLICTS` and `IMPACT: DONE`. An answer without
   these parts is not used (the run fails and is retried later).
5. The worker posts one Jira comment with the assessment and finishes the run with outcome `assessed`; the parsed
   assessment is stored in the run's `details.impact`, which `GET /v1/business/<project>/change-requests` returns
   to the screen.
6. The ticket stays in Backlog. The owner reads the assessment and moves the ticket to Intake in Jira when it should
   go ahead; from there the normal intake flow rewrites it into the template.

## Guarantees

- An impact run only comments: the control API refuses its transitions, field and label writes and description
  writes (403), and the policy hook keeps it read-only (no file edits, no installs), like an intake run.
- Neither the Business form nor the impact step moves a ticket to Intake or To Do.
- `GET /v1/business/<project>/change-requests` also returns the project's epics and the Jira site for links.

Tests: `tests/v4/test_business.py`.
