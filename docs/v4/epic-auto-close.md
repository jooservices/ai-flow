# Automatic epic completion

During each `jira_sync`, AI Flow checks the mirrored child tickets of every epic in the configured Jira projects.
An epic with at least one child moves to **Done** when every child is Done. If a child is later added or reopened
while the epic is Done, the epic moves back to **To Do**. Child tickets nested under another ticket are included
when their parent chain leads to the epic. An epic with no children is left unchanged.

When Flow moves an epic to Done, it comments with the child count, UTC completion time and a link to the HTML
epic report. If that completes the project's last open epic, the comment also suggests that the lead consider
closing the project and links to the project report. Flow does not close the project.

Project and epic reports expose `project_close_suggestion` with `suggested`, `epics_done` and `epics_total`.
The JSON response and the Markdown and HTML exports say to consider project closure only when the project has at
least one epic and every epic is Done. This also covers projects whose last epic was moved to Done outside Flow.
