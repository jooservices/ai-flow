# Only real business decisions reach a person

**Console screens:** Command Center (`cc`) and Machines and AI (`machines`).

A person is needed only for **new business decisions** and **irreversible or production actions**. Everything else a
ticket might stop on is classified when it is raised and resolved by AI Flow itself. This builds on
[Needs owner triage](triage.md), the [master](master.md), the policy hook and the knowledge register.

## Classes (AC1)

Every escalation of a developer run is classified when it is raised (`POST /v1/escalations/raise`, called by the dev
worker when the AI ends with `RESULT: BLOCKED`), and every triage decision is classified again when it is applied.
The rules are deterministic (`aiflow/v4/control/escalations.py`), so classification needs no model run.

| Class | Example | What happens | Person |
|---|---|---|---|
| `missing_tool_or_environment` | a browser, Docker, a CI log, or a reportable catalogue/stack requirement | the machine's tools are checked: a tool the machine has is answered ("installed, use it"), a CI log is supplied (AC6), and a reportable missing requirement is parked ([missing tools](missing-tools.md)); Dev VM, SonarQube and devices have no heartbeat probes yet, so reports naming them fail without creating a wait | no |
| `policy_message` | `p='src/auth/provider.ts'` refused by the command policy | the run is answered with the reason and the allowed alternative (AC5) | no |
| `unreachable_criterion` | "AC4 needs a smoke test on the Dev VM; there is no Dev VM" | the ticket is amended (AC4 below) | no |
| `ambiguity_repository` | "which existing helper formats dates?" | the triage AI answers from the ticket, spec and code | no |
| `decision_already_taken` | "may I use Docker?", "may I tag the release?" | the register entry is applied and cited (AC2) | no |
| `new_business_decision` | "email the customer immediately or after 24 hours?" | asked once with options and a recommended default (AC3) | yes |
| `irreversible_or_production` | dropping a table in production, rotating production credentials | asked once, the same way | yes |

A question that is resolved without a person goes back to **To Do** with a comment (What, Why, Result, Next) that
names the class and cites the register entry. The run ends with outcome `auto_answered` (or `tool_missing` when the
ticket was parked); the next developer run gets the question and the answer in its prompt. The triage AI may not
escalate a question of one of the five other classes: the control plane refuses it and the run is retried.

## The owner decision register (AC2)

`owner_decisions` holds what the owner has already decided. It starts with: no Docker for AI runs, no release or
tag without the owner, name the company JOOservices, plans and limits (Free, Team, Business) and the quality
thresholds (coverage at least 90 percent). Each entry has patterns (regular expressions, or "every one of these
keywords"), an optional project, who decided and when.

- A matching entry is applied automatically, cited in the comment ("Owner decision D1 "No Docker for AI runs", owner,
  2026-10-23") and counted (`cited_count`, shown as "Applied n×").
- The triage AI sees the register in its prompt and in the claim (`owner_register`), and may not escalate what it
  answers.
- The owner adds a decision in **Command Center → Escalations and owner decisions → Record a decision** (title, the
  decision, two or more keywords) or retires one. Both are audited (`write_audit`, target `escalation`).

## A new decision is asked once (AC3)

The triage AI ends with `DECISION: ESCALATE`, a valid `CLASS` (`new_business_decision` or `irreversible_or_production`),
`WHY`, at least two `OPTIONS`, a `RECOMMENDED` default, a `TOPIC` and `KEYWORDS`. Without options, a recommendation,
or a valid class, the output is invalid and retried. The control plane uses the class and question stored when the escalation
was raised: it rejects an `ANSWER` for either person-facing class and rejects `ESCALATE` for any of the other five.
Triage output cannot relabel a question to bypass that boundary. The control plane then:

1. applies a **decided** register entry that matches (nothing is asked);
2. otherwise, if the same normalized question is already **asked**, adds the ticket to that wait (no second alert;
   the match does not depend on triage-generated keywords);
3. otherwise stores a pending entry (`status asked`, options, recommended default), comments with the options on the
   ticket, labels it `owner-escalated` and alerts the owner once.

The owner picks an option (or the recommended default) on screen. The entry becomes **decided**, and every waiting
ticket loses the label and returns to To Do with the decision cited. If the owner answers in a Jira comment and moves
the ticket back instead, the next developer claim reads a comment posted after the ask only when its stable Jira
`accountId` matches the account connected in Integrations (the authorized owner identity). Display names are not used
for authorization. The actual answer is stored in the register and passed with the question to the developer prompt.
The recommended default is never substituted for a missing or unauthorized comment; the ask stays pending until an
authorized answer is available. Other waiting tickets resume with the captured answer.

## Unreachable criteria are amended (AC4)

When a question says an acceptance criterion (`ACn`) cannot be met here because the evidence needs an environment that
does not exist (a Dev VM, SonarQube, a device), triage amends the ticket itself: the line `- ACn: …` in the
description is narrowed to what a machine of the fleet can verify (or removed, `NARROWED: REMOVED`), keeping the
original text and a tag `[narrowed 2026-10-23, register D12]`. The amendment is a register entry of kind `amendment`,
the ticket goes back to To Do, and nobody is asked. When the criterion cannot be found in the text, the triage AI
decides with `DECISION: AMEND` (`CRITERION`, `NARROWED`, `WHY`). Amendments are listed in the weekly report and in
Command Center for review; they never block.

## Refusals explain themselves (AC5)

Every refusal of the policy hook (`/v1/policy/decide`) ends with `Allowed instead: …` and "This is a policy message
about this one call, not a restriction that needs a person". Examples: "commands must start with an allowed program
(…); use the Edit or Write tool to change files and the Read tool to read them"; a command that starts with a
variable assignment (`p='src/auth/provider.ts'`) is explained as such; `git commit` and `git push` say AI Flow commits
and pushes the work; a write outside the worktree names the Edit or Write tool with a path inside it. The dev prompt
says the same. Allowed calls are unchanged.

## CI log tail (AC6)

`GET /v1/github/ci-log?run_id=…&fence=…&branch=…&lines=200` (node token and valid fence, audited) returns the tail
(at most 1,000 lines, 20,000 characters, colours and timestamps removed) of the first failing GitHub Actions job of
the run's recorded PR branch: job, workflow, failed steps, URL and `tail`. Only a developer run with a recorded PR
can use it; a caller-supplied branch must match the run's recorded PR branch, and a supplied SHA must match the PR's
current head commit as confirmed with GitHub. Rework runs inherit the PR identity from the previous developer run.
AIs cannot reach the control API themselves,
so the dev worker calls it before a rework run and puts the tail into the prompt: the AI never has to ask for the log.
The GitHub App needs the `actions: read` permission.

## Numbers (AC7)

`GET /v1/escalations` (and `escalations` in `GET /v1/master/report`) return, for the last eight UTC weeks, escalations
per class and per project in each week, the share resolved without a person, whether this week is below last week,
the **top three causes with the fix for each** and the amendments for review. Open and waiting records do not count
as resolved without a person; a waiting ticket records its owner resolution when the shared answer arrives. An owner
choice is saved before Jira updates begin; failed ticket updates stay visible as resuming and the control loop retries
them. Command Center shows them in the panel *Escalations and owner decisions* (weekly class table, week-by-project
table, the open questions with one button per option, the register, and the amendments). The target is a falling line.

## Tests

`tests/v4/test_escalations.py` covers each class, the register (match, citation, no repeat), the one-time ask and its
conversion into an entry, amendment, refusals, the CI log, the weekly numbers, and replays the escalations of
2026-10-22 and 23 recorded in `tests/v4/fixtures/escalations-2026-10-22-23.json` (JAIF-130, JAIF-220, JAIF-222,
JOOWP-301/303/304/308/312 and the rest of the same families): each is classified and none of the five other classes
reaches a person.

## Control API paths

The developer worker records a stop with `POST /v1/escalations/raise`; the master applies its answer with
`POST /v1/escalations/triage`. `POST /v1/work/claim` passes the decision register and any saved answer into a run.
The policy hook responds through `POST /v1/policy/decide`. Command Center reads the register and weekly metrics from
`GET /v1/escalations`, records decisions with `POST /v1/decisions`, answers with
`POST /v1/decisions/{decision_id}/answer`, and retires entries with
`POST /v1/decisions/{decision_id}/retire`. Escalation totals are also included in `GET /v1/master/report`; the
master snapshot is available through `GET /v1/master/autonomy`.

Other worker Jira writes stay fenced to the claimed ticket: `POST /v1/jira/{key}/comment` posts a comment, and
`POST /v1/jira/{key}/fields` updates its fields or labels. Both require the run ID and fence; an impact run may
comment but cannot change fields.
