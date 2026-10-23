# User guide: Owner

**Assumes:** you are signed in to Command Center with the `owner` role and AI Flow v4 is installed. You can
manage integrations, nodes, projects and knowledge, but you cannot manage console users.

## What you can do

| Task | Screen |
|---|---|
| Watch the whole flow and its incidents | [Command Center](../screens/cc.md) |
| See what is in progress for one project | [Delivery](../screens/delivery.md) |
| Connect Jira and GitHub | [Integrations](../screens/integrations.md) |
| Add, pause, resume, remove projects | [Projects](../screens/projects.md) |
| Approve or send back finished work | [Human review](../screens/review.md) |
| Open the test environment and record acceptance | [Stakeholder](../screens/stakeholder.md) |
| Manage study cases, skills and policies | [Knowledge](../screens/knowledge.md) |
| Follow epics and change requests | [Business](../screens/business.md) |

## First day

1. Connect Jira on [Integrations](../screens/integrations.md).
2. Connect GitHub on the same screen.
3. Add your first project on [Projects](../screens/projects.md). The preflight tells you what is missing.
4. Join a node from Command Center (**Add node**). The token is valid for five minutes and works once.
5. In Jira, put a ticket in To Do with the label `aiflow-ready` and watch it appear on [Delivery](../screens/delivery.md).

## When something goes wrong

An incident appears in Command Center and critical ones are emailed. Open the [runbook](../runbooks/index.md) for
its kind: it says what you see, why it happens and what to do.
