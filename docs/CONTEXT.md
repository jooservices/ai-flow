# AI Flow: the context guide

Read this first. It explains what AI Flow is, why it exists, how a piece of work travels through it, the rules everybody follows, and how to work with the owner. It is deliberately not technical: for commands, settings and internals see `README.md` and `docs/RUNBOOK.md`.

## 1. In one paragraph

AI Flow is a hands-off delivery line. The owner writes tickets and decides. When a ticket is ready, AI workers build it, other AIs review and test it, and a supervisor merges it, with the issue tracker as the single record of everything. People are asked only for decisions: an approval, an answer, a hold. The goal is that routine work moves from "ready" to "merged and verified" without anyone waiting for anyone.

## 2. Why it exists, and what success means

- Routine delivery work is repetitive, depends on who is free, loses context between people and tools, and stops when people stop. A fixed, checked path removes that.
- Success is measured by: the share of tickets that reach Done with no human action, the number of rework rounds per ticket, the time from "ready" to merged, and traceability (every decision visible in the tracker, no merge without passing checks).
- Not in this version: preview environments for approvers, more than one worker machine, publishing to a wiki.

## 3. Words that mean something specific

| Word | Meaning |
|---|---|
| ai-flow | This project: the system that builds the delivery line. |
| AI workflow | The general idea of delivering work with AIs. Do not mix the two. |
| Owner | The person who writes and approves tickets. Product owner and CEO, not an executor. |
| Ticket | One unit of work in the issue tracker. The queue is the tracker itself. |
| Worker / supervisor | The always-on program that watches the tracker, starts AIs, checks pull requests and merges. |
| Developer AI, reviewer AI, QA AI | The three roles an AI can play on a ticket. Never the same AI for developer and reviewer, nor developer and QA when another AI is available. |
| Gate | The ordered checks a pull request must pass before merge. |
| Rework | Going back to the developer with exact reasons after any failed check. |
| Blocked | Waiting for the owner, with a stated reason. |
| Container / leaf | A ticket with children is a container; only leaves are worked. |
| Hotfix, release | Work that targets the production branch instead of the integration branch. |
| Drill | A real ticket run end to end to prove one path of the flow. |

## 4. The parties

- **The owner** writes tickets, answers questions, approves risky changes, can hold or pause anything. Never merges by hand.
- **The issue tracker** is the queue and the record. A ticket's status says where it is.
- **The workers** (supervisors) run on one machine today, around the clock. They pick tickets, start AIs, open pull requests, check CI, mark drafts ready, merge, and clean up.
- **The AI agents** do the thinking work in isolated copies of the code: develop, review, test. They can read and write code in their copy and comment on the ticket. They cannot write to the code host.
- **The code host** holds the repository, runs automated checks, and hosts review bots.
- **A small state store** keeps locks, heartbeats and counters so two workers never take one ticket. It is not the source of truth; the tracker is.

## 5. The journey of a ticket

1. **Backlog.** The owner writes the ticket (often after brainstorming with an AI). Nothing happens yet.
2. **To Do.** The owner moves it here when it is complete. A worker picks it up in priority order (rework first, then Highest priority, then oldest). A ticket missing acceptance criteria, scopes or tests is sent back to Backlog with a note.
3. **In Progress.** The developer AI posts a short plan, works in an isolated copy from the latest base, tests, and leaves a hand-over comment: files changed, why, deployment steps, lessons.
4. **In Review.** A different AI reviews against the acceptance criteria and answers with a checklist, then PASS or FAIL. FAIL returns the ticket to To Do with reasons.
5. **Review passed.** The supervisor (no AI) opens the pull request as a draft, filled from the repository's own template and the developer's hand-over.
6. **In QA (the gate).** The supervisor walks eight stations: branch current with its base; CI green; draft marked ready so review bots comment; every feedback item answered (fixed, or rejected with evidence); independent QA of the exact commit; approval if a person is required; merge of that exact commit; clean-up of the work copy and branches. If the base moves, the branch is updated and QA runs again.
7. **Human review and Ready to merge.** Only when approval is required (a ticket field, a deployment step in the hand-over, or a release). The owner moves the ticket to Ready to merge, and the supervisor merges.
8. **Done.** Merged and cleaned up. A container becomes Done by itself when all its children are Done.

Side paths: any failure goes back to To Do labelled `rework`, with the exact reasons; after several rounds without success the ticket is Blocked. Anything unclear is Blocked with a reason; the owner answers in a comment and moves the ticket to To Do, which starts a fresh AI session that reads the answer.

## 6. The rules that never change

1. The tracker is the queue and the record. State is never hidden in a tool.
2. Each AI step starts fresh from the ticket and its history. Nothing relies on an AI remembering.
3. Checks are independent: the reviewer and QA are not the developer.
4. A change merges only when it is up to date with its base and has been tested on that exact state.
5. Every feedback item gets an outcome: fixed, or rejected with a reason. Rejections are written by the developer AI.
6. People decide, AIs execute. Nobody merges by hand.
7. AIs never write to the code host. The supervisor does every post, merge and clean-up. (An AI that posts there is read back as the owner, because everyone shares one account.)
8. Only accounts with write access can give feedback an AI acts on.
9. AIs never change the operating system, the machine environment, tools or containers. Shared services are used as they are.
10. If an AI is not ready (signed out, out of balance, out of usage), another AI replaces it. If none is available for 30 minutes the ticket is Blocked and resumes by itself.
11. Everything written in the tracker, the code host and the code is in English.

## 7. Writing a ticket an AI can act on

- Use the template: business requirements (this ticket only), acceptance criteria, scopes, tests, technical approach, and for tasks a code structure.
- Child tickets do not repeat the parent: read the parent chain up to the epic first.
- Hierarchy: an epic contains stories, tasks and bugs; those contain sub-tasks. A story cannot contain tasks.
- Useful fields: type (feature, bug, hotfix, chore, docs, refactor), priority (Highest goes to the strongest AI, first), "require human", and fields to name a specific AI or model.
- A ticket must carry everything the AI needs. The AI cannot ask questions; if it cannot understand, it blocks the ticket and says why.
- Holding a ticket out of the flow: use the hold label. Moving a ticket to To Do or In Progress starts work immediately, so do it only when you mean it.

## 8. Hotfix and release

- **Hotfix:** starts from and targets the production branch, follows the same review and tests, and after the merge a ticket is created automatically to merge production back into the integration branch.
- **Release:** starts from the integration branch at a fixed point. The AI runs the team's full test scripts (it never invents tests); each failure becomes a bug ticket and stops the release. If all pass, it prepares the changelog, documents and version. The owner always approves; then the supervisor merges, tags and merges back. There is no release planned right now.

## 9. How AIs are chosen

Ticket names an AI: that one. Priority Highest: the strongest AI. Otherwise the default for the role. The AI must be signed in and within its limits (checked regularly); if not, the next one takes the step. A free low-cost AI works only when a ticket names it. Usage limits put an AI on a cooldown shared by all workers.

## 10. What you must do and must not do (if you are an AI on a ticket)

- Do: read the ticket, its comments and its parents; post a plan before coding; work only in your isolated copy; keep commits in the repository's convention; write the hand-over in the template; answer every feedback item; finish with the required result lines.
- Do: if a command is rejected, find another way inside your copy; never bypass a guard.
- Do not: write to the code host, push, merge, change ticket status (the supervisor does), touch the system, tools or containers, print secrets, or guess when a decision belongs to the owner. Block the ticket with a precise question instead.
- Reviewers: do not fail a change because the branch is behind, CI has not run or the pull request does not exist yet; the supervisor handles those.

## 11. Working with the owner

- The owner is the product owner and CEO and writes in Vietnamese; speak Vietnamese to the owner, write English in the tracker, the code host and the code.
- When the owner is brainstorming, advise and audit first; do not implement until asked. When the owner says to do it, do exactly that, with no extras and no repeated questions. Do not hand commands back: execute.
- Never give feedback or claim a limit without checking, auditing and verifying first. Report outcomes honestly, including failures and what was skipped.
- Decisions already made, do not reopen: the tracker is Kanban without sprints; unblock always means moving to To Do; pull requests open as drafts; reports and artifacts for presentation are generic (no project or environment names); versions of tools are LTS or most stable, no need to ask; the owner never merges manually.

## 12. Where things stand

- Proven on real tickets: the whole path with no human step, human approval before merge, two tickets editing the same file, a ticket without the template, Highest priority routing, an unclear ticket then an answer, a container closing itself, a hotfix with the automatic merge-back.
- Running continuously on one worker machine (a control service keeps the workers of each project equal to the configuration) with health checks, a pause switch, AI sign-in probes and a daily usage summary.
- Not built: preview environments for approvers (a staging deployment is planned), a second worker machine, wiki publishing. Projects may opt into isolated per-ticket databases with their `database` TOML setting; the shared state store moves to the production one only when the owner announces the release of ai-flow.
- Next: the first real project (javmeta) will be used to test the flow, in another working session, starting with approval on and auto-merge off for the first week.

## 13. Where to look

- `README.md`: how everything works, settings and commands.
- `docs/RUNBOOK.md`: day-to-day operation, blocked reasons, onboarding a project.
- `docs/OPERATOR.md`: what the flow does alone when it hits a problem and what always goes to the owner.
- The workspace skill `workspace-ai-flow`: the short version for AIs working in the workspace.
- The presentation of the design (20 slides): shared with the owner.
