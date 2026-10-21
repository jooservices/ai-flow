# Issue types and labels

AI Flow v4 intake rewrites each ticket into a template selected from its Jira issue type. Every template uses the
four shared sections below, in order. Type-specific sections follow **Business requirement**; the remaining shared
sections keep their order. Each acceptance criteria list starts at `AC1`.

| Jira issue type | Template sections, in order |
|---|---|
| Epic | Business requirement; Acceptance criteria; Technical notes; Definition of done |
| Story | Business requirement; Acceptance criteria; Technical notes; Definition of done |
| Feature | Business requirement; Acceptance criteria; Technical notes; Definition of done |
| Bug | Business requirement; Steps to reproduce; Expected result; Actual result; Environment; Acceptance criteria; Technical notes; Definition of done |
| Hotfix | Business requirement; Steps to reproduce; Expected result; Actual result; Environment; Acceptance criteria; Technical notes; Definition of done |
| Tech Debt | Business requirement; Problem; Risk; Acceptance criteria; Technical notes; Definition of done |
| Chore | Business requirement; Acceptance criteria; Technical notes; Definition of done |
| Spike | Business requirement; Question; Timebox; Output; Acceptance criteria; Technical notes; Definition of done |
| Docs | Business requirement; Reader; Acceptance criteria; Technical notes; Definition of done |
| Security | Business requirement; Threat; Impact; Acceptance criteria; Technical notes; Definition of done |
| Sub-task | Business requirement; Acceptance criteria; Technical notes; Definition of done |

For example, a Bug description starts like this:

```text
Business requirement: <why this fix is needed>
Steps to reproduce: <steps>
Expected result: <what should happen>
Actual result: <what happens now>
Environment: <affected environment and versions>
Acceptance criteria:
- AC1: <one observable, testable behaviour>
Technical notes: <implementation constraints and project rules>
Definition of done:
- <tests, checks and documentation needed>
```

The analogous extra sections for Hotfix are the same as Bug. Tech Debt records the existing problem and its risk;
Spike records a question, timebox and expected output; Docs names its reader; Security records the threat and
impact. If a needed detail cannot be established from the ticket, comments or project repository, intake asks the
owner rather than inventing it. It can mark a section as not applicable when that detail is not needed for clear,
testable work.

## Jira issue type fallback

The Jira mirror currently defaults a missing issue type to `Task`. `Task` and every other type outside the list
above use the Story template. Intake does not change the Jira issue type while rewriting the description. `Sub-task`
remains available for work split from a parent ticket and uses the shared sections.

## Labels intake recognizes

Intake receives the ticket's labels, recognizes the following exact labels and patterns, and preserves all labels
as Jira metadata. It adds `aiflow-ready` after a successful rewrite, as before. Unknown free-form Jira labels are
also preserved and ignored by intake.

| Label | Current v4 meaning |
|---|---|
| `aiflow-ready` | Queues a ticket in To Do for development. Intake adds it after a successful rewrite. |
| `aiflow-hold` | Holds a ticket out of AI work, including intake and development. |
| `rework` | Gives a ticket priority among otherwise eligible development work. |
| `require-human` | Requests human review and notification when that review starts. |
| `tier-senior` | Restricts development to a senior-tier account and selects its senior model. |
| `ai-<provider>` | Restricts work for `explicit_only` accounts to tickets labeled for that provider, such as `ai-grok`. |
| area-&lt;core&#124;theme&#124;plugin&#124;platform&#124;flow&gt; | Identifies the affected AI Flow area. |
| `regression`, `uat-bug`, `blocked-external`, `needs-design`, `breaking-change`, `perf`, `a11y`, `seo`, `security` | Classification labels for the ticket. |

The routing behavior of `aiflow-ready`, `aiflow-hold` and `tier-senior` stays unchanged. Labels provide context;
they do not authorize intake to add requirements that are absent from the ticket.

## Jira issue type setup plan for TESTFLOW-50

Jira issue types are administrative configuration, so this ticket prepares the following plan for the owner to
review and run with TESTFLOW-50. No Jira types or label definitions are created by the v4 application. Jira labels
are free-form and need no setup.

1. The owner reviews the proposed names: Epic, Story, Feature, Bug, Hotfix, Tech Debt, Chore, Spike, Docs and
   Security. Keep the existing `Sub-task` type for splitting work; keep `Task` if the Jira instance uses it, since
   intake maps it to the Story template.
2. During TESTFLOW-50, a Jira administrator checks the instance's existing issue types and the target project's
   issue type scheme. Reuse matching types and create only missing types. Add the new types to the target scheme;
   use standard issue types except for the existing Sub-task type. Confirm Epic/parent linking remains available.
3. Confirm the scheme's screens expose the fields AI Flow mirrors and writes: summary, description, priority,
   labels and parent where applicable. The type-specific details live in the description, so no Jira custom fields
   are required for this proposal.
4. After the owner approves and the administrator applies the changes, create or edit one sample ticket per type,
   sync it into AI Flow, and confirm the mirrored type selects the matching intake template.

If the owner does not approve additional Jira types, the v4 code still handles the existing type names and applies
the Story template to unsupported types such as `Task`.
