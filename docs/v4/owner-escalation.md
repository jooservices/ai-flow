# Owner escalation messages

## Jira hand-off

When triage chooses `ESCALATE`, the Jira comment begins with the question, lists each available option and names the
recommended option. The ticket receives `needs-owner` (and the compatibility label `owner-escalated`) and remains in
the v4 **Needs owner** status, which is the configured blocked/waiting state. The owner answers in a Jira comment and
moves the ticket to **To Do**. Before creating a development run, `POST /v1/work/claim` verifies that the connected
Jira owner has commented after the question. If no verified answer is available, the claim is refused and the ticket
is returned to **Needs owner**. After a verified answer, AI Flow clears the owner labels and passes the answer to
development; it also applies that answer to other tickets waiting on the same decision. The Command Center cannot
answer a ticket-linked owner question or resume it without that verified Jira comment.

The escalation stays open and triage-eligible until Jira confirms the question comment and both labels. If a comment
or label write fails, the next triage run reuses the pending owner decision and any recorded question comment, retries
the incomplete writes, and closes the escalation only after the required Jira state is present.
The rendered question body sent through the hand-off route, including answer instructions and any grouping note, is
limited to 6,000 characters; triage refuses a larger or incomplete decision before saving it as pending.

On upgrade, linked decisions marked decided by an older Command Center remain pending until a Jira answer is
verified. The migration marks existing hand-offs eligible for legacy recovery; only those rows can match an older
Jira question comment without its correlation marker, and recovery still requires the full formatted question,
options, recommendation and answer instructions. Recovery reads the paginated, untruncated Jira history and ignores
comments from before the current escalation was created.

## Optional message channel

Direct notification is off unless a complete channel is configured in the control plane's `.env` or a file named by
`AIFLOW4_SECRET_FILES`. The notification body contains only the question and its Jira issue link. SMTP uses the
question as the subject and sends the question plus link as the body. Telegram sends the question and link together.
Provider tokens and SMTP credentials are used for delivery and are never included in the message.

For email, set `AIFLOW4_ESCALATION_EMAIL`, `AIFLOW4_ESCALATION_SMTP_HOST` and optionally
`AIFLOW4_ESCALATION_SMTP_FROM`, `AIFLOW4_ESCALATION_SMTP_PORT`, `AIFLOW4_ESCALATION_SMTP_TLS_MODE`,
`AIFLOW4_ESCALATION_SMTP_USERNAME` and `AIFLOW4_ESCALATION_SMTP_PASSWORD`. The sender defaults to the recipient,
the port defaults to 587, and TLS mode defaults to `starttls` (`ssl` and unauthenticated `none` are also supported).
SMTP authentication with a username requires `starttls` or `ssl`.

For Telegram, set both `AIFLOW4_ESCALATION_TELEGRAM_CHAT_ID` and
`AIFLOW4_ESCALATION_TELEGRAM_BOT_TOKEN`. If both channels are complete, Telegram is selected. An incomplete
configuration sends nothing.

The first question can send immediately. Further questions wait in PostgreSQL and are grouped into one message when
the one-hour limit expires. The limit is shared across control processes, and the control loop flushes the group even
when no later escalation arrives. A repeated question joins its existing owner decision and does not send a second
immediate alert.

The fenced triage worker posts the Jira question through `POST /v1/escalations/{key}/question-comment`, which checks
the triage run and fence, validates the submitted text against the pending ticket question and registered options,
recommendation and grouping state, then records Jira's actual comment timestamp. A mismatch is rejected before Jira
lookup or write. It requests delivery through
`POST /v1/notify/triage` before writing the labels, so a failed queue request leaves the open escalation eligible for
triage recovery. It then completes the hand-off through
`POST /v1/escalations/{key}/complete-handoff`. The completion route requires the pending decision, comment timestamp
and mirrored labels; repeated requests are safe. Only an owner comment created after that question timestamp can
authorize the next development claim.

The Command Center reads the decision register from `GET /v1/escalations` and records register entries through
`POST /v1/decisions`. `POST /v1/decisions/{decision_id}/answer` remains available for register entries that are not
linked to a Jira ticket; ticket-linked questions require the verified Jira comment described above.
The Console also loads ticket context through `GET /v1/tickets/{key}` and service health through `GET /v1/services`.

The question comment carries its stable owner-decision ID so a retry with a different triage run can recover the
timestamp from the same Jira comment. The endpoint still checks the current triage run and fence before posting. The
related control workflow uses `GET /v1/master/candidate` to check for a master monitoring cycle and
`GET /v1/work/candidates` to offer regular work. It uses `POST /v1/escalations/raise` to record the initial developer
question, `POST /v1/escalations/triage` to record the triage decision,
`POST /v1/jira/{key}/fields` to mirror owner labels, and
`POST /v1/runs/{run_id}/result` to record the triage result. Reviewer questions enter through
`POST /v1/reviews/{key}/ask-ai`; an obsolete owner decision can be retired with
`POST /v1/decisions/{decision_id}/retire`. Retirement is rejected while a ticket-linked decision still needs its
verified Jira answer, preserving the development claim gate even when the ticket's owner labels were cleared.

## Architecture and checks

Escalation policy stays in `control/escalations.py`; escalation and ticket persistence updates live in their
repositories. Fenced owner-question and notification endpoints live in `control/routes/owner_escalation.py`, mounted
by route discovery. Sender selection and grouping live in `control/owner_notifications.py`, while the durable queue
and hourly slot live in `control/repositories/owner_notifications.py`. The control loop flushes due groups, and the
migration registers the queue state in the control database. No worker, node or core module imports control.
