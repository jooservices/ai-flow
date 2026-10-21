# Stakeholder review

Project deployment links live in the control repo at `projects/<KEY>.yaml`. Configure either environment or
both; an omitted mapping, key, or empty value is treated as not deployed:

```yaml
key: TESTWP
repo: jooservices/test-aiflow-wp
urls:
  develop: https://uat.example.test
  production: https://www.example.test
```

Open `/stakeholder` in the v4 control console and select a project. The screen links to the configured develop
(UAT) and production pages in a new tab, or displays `chưa deploy` when a URL is empty. It lists each ticket with
a completed successful development run, its related epic when configured, and the latest release or commit
recorded by that run. Refresh reloads this list.

For each change, choose the environment, release or commit, and sender. **Đúng mong đợi** adds a comment
nghiệm thu to the selected related ticket or epic, including the environment, release or commit, and sender.
It does not create an issue.

**Chưa đúng mong đợi** requires the expected and actual behaviour and creates one Bug in the same Jira project.
The description includes the page URL (or `chưa deploy`), environment, related ticket, release or commit,
expected behaviour, actual behaviour, and sender. The issue receives the `uat-bug` label and moves to Intake.
A selected PNG, JPEG, or WebP screenshot is attached; it is optional. The screen links to the new issue key
after Jira confirms the write. A Jira error is shown as a failed submission.

The read API is `GET /v1/stakeholder/<KEY>`; verdicts are submitted to
`POST /v1/stakeholder/<KEY>/verdict`.
